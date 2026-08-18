"""Node: the AlLoRa node. One object carrying identity, config, and BOTH whole-file loops.

A transfer always has a *drive* side (the initiator: polls, asks METADATA/CHUNK, reassembles)
and a *serve* side (the responder: lives with the data, answers each request). Historically
those were two classes (Requester drives, Source serves), which made role reversal need two
mirrored node instances copying state between them, the chief fragility of the old role-swap
experiment. Here both loops live on one node with one config, one connector and one session
state; `current_role` ("collector" drives, "source" serves) picks which loop runs, so
reversing a role never constructs, mirrors or synchronizes a second node.

That is why the role is a mode on a live node and not a type: the state a loop needs (the
radio, the session, the RF config and its trial, the identity keys, the pacing controller) is
node-scoped and shared, and the code that *moves* between the roles (delegate, grant, yield)
belongs to neither side. Splitting the loops into separate objects would put that machinery
outside both and force them to reach back into the node for everything they touch.

The node-type set is exactly {Edge, Hub}, and both are thin placement presets on this class:
each picks a home role and adds the loop it runs by default. An Edge serves by default, a Hub
drives by default. No swap state is ever persisted. On boot a node is back at `home_role`, and
the Hub resuming its poll re-converges the pair after any failure.
"""
import gc
from os import urandom
from json import loads, dumps

from AlLoRa.Packet import Packet
from AlLoRa.Packet_v3 import Packet_v3
from AlLoRa.Connectors.Connector import Connector
from AlLoRa.Status import Status
from AlLoRa.File import AlLoRa_File
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.Control.control_types import IN_BAND, KNOWN as CONTROL_TYPES
from AlLoRa.Pacing import Pacing
from AlLoRa.utils.time_utils import get_time, current_time_ms as time, sleep, \
    ticks_add, ticks_diff
from AlLoRa.utils.debug_utils import print
from AlLoRa.utils.os_utils import os
from AlLoRa.utils.file_utils import commit_file
from AlLoRa.utils.json_utils import json


class Node:

    # What asking a peer to change its radio configuration can come to. Four outcomes and not
    # two, because delivering a command is not the same as having it taken, and a peer that
    # says no is not the same as a peer that is not there. A boolean forced the caller to
    # guess between them, and it guessed optimistically: a refusal read as success, and a
    # policy refusal read as a broken link, which sends someone to a site when the repair is a
    # missing key. Each value names a different next move for whoever reads it:
    #
    #   ACCEPTED    both ends are on the new configuration. Nothing left to do.
    #   REFUSED     the peer heard the command and declined it. Look at its provisioning.
    #   PENDING     a signed artifact was handed over; the probe decides later which way it
    #               went. The only honest answer while the verdict is still on the far node.
    #   UNREACHABLE nothing answered at all, so the command was never even considered. Look
    #               at the link.
    #
    # Plain lowercase strings rather than an enum or an int: they cross into a backend's JSON
    # and onto an operator's screen unchanged, and MicroPython has no enum to lean on anyway.
    ACCEPTED = "accepted"
    REFUSED = "refused"
    PENDING = "pending"
    UNREACHABLE = "unreachable"

    # ask_data's third answer: the peer replied to a chunk request by announcing what it is
    # serving, because it does not remember announcing the file being assembled. Neither a
    # chunk nor the None that means nothing came back, and it must not be confused with the
    # latter: silence is a lost frame and is repaired by re-asking, while this is the peer
    # saying that re-asking is pointless.
    RE_ANNOUNCED = "RE_ANNOUNCED"

    def __init__(self, connector: Connector = None, config_file="LoRa.json",
                 debug_hops=False,
                 max_sleep_time=3,
                 successful_interactions_required=5,
                 data_sink=None,
                 datasource=None,
                 control_actuator=None,
                 home_role="source"):
        self.config_file = config_file
        self.open_backup()
        self.connector = connector

        # What an in-band control command acts through. The signed route reaches its actuator
        # inside the verify gate, which is a DataSink and so arrives with the file; an in-band
        # command has no file and no gate, so the node holds the reference itself. A node given
        # none simply does not accept in-band control, which is the right default: an actuator
        # is a capability an operator grants, not one a node has by existing.
        self.control_actuator = control_actuator

        # The control root this node was provisioned with, if any. One name for both halves of
        # the key: the minting private half on an authority that issues artifacts, the pinned
        # public half on a node that only verifies them. Holding either is what makes this node
        # part of a signed control deployment, and that is the whole reason the base class
        # carries the reference: an unsigned in-band command must be refused by a node that has
        # a stronger tier available, or provisioning one would secure nothing.
        self.control_root = self._configure_control_root()

        # Where this node keeps its control counter: the highest number it has minted on a Hub,
        # the highest it has accepted on an Edge. One key for both kinds, named beside the root
        # because it belongs to the same provisioning act, and resolved here rather than by
        # whatever assembles the verify gate. An Edge is not always the radio board: it is just
        # as often the logic-holder on a host with a real filesystem, where the working
        # directory may be read-only or cleared at every start, and moving the mark somewhere
        # that survives is one line of config. Read only by the example, that line would do
        # nothing on a node someone wrote themselves, and nothing would say so.
        self.control_counter_file = self.config.get('control_counter_file', 'control.counter')

        self.LAST_IDS = list()              # IDs from my mesagges
        self.LAST_SEEN_IDS = list()         # IDs from others
        self.MAX_IDS_CACHED = 30            # Max number of IDs saved

        # An RF-config change arms a trial: run on the new config, and either commit it (a full
        # exchange proves it works) or self-restore to the last-known-good on a silent/stalled
        # window. `sf_trial` is the armed flag; the window is time-based (seconds), carried in
        # the signed RF_CONFIG payload (`trial`) with a ToA-scaled default. The deadline is armed
        # lazily by the serve/drive loop so the window counts from when the node starts running
        # on the new config, not from the flash write.
        #
        # `_trial_ceiling` is the second, harder deadline: any short control frame heard on the
        # new config pushes `_trial_deadline` out, so a peer that keeps polling (an asymmetric
        # link whose replies are lost re-asks forever) would otherwise hold an unproven config
        # indefinitely, committing never and restoring never. Nothing moves the ceiling.
        self.sf_trial = None
        self._trial_window_s = None
        self._trial_deadline = None
        self._trial_ceiling = None
        self.TRIAL_CEILING_WINDOWS = 3      # how many windows a trial may last, holds included

        # The observability surface: the live values plus the subscribers they are pushed to.
        # It is a plain holder with no protocol in it, so a bridge board can be observed the
        # same way without being a node.
        self.status = Status()

        self.config_connector()

        # What THIS node's own config file asked for, snapshotted before any endpoint can
        # retune the radio. It is the fallback an endpoint that states no RF resolves to, so
        # it has to be the configured values and not the live ones: mid-round the radio is
        # sitting on whichever endpoint was visited last.
        self.rf_defaults = self.connector.get_rf_config()

        self.status["Freq"] = self.connector.frequency
        self.status["SF"] = self.connector.sf
        self.status["BW"] = self.connector.bw
        self.status["CR"] = self.connector.cr
        self.status["TX_P"] = self.connector.tx_power

        self.status["Status"] = "WAIT"  # Status of the requester
        self.status["RSSI"] = "-" # Signal strength
        self.status["SNR"] = "-"  # Signal to Noise Ratio

        self.status["Chunk"] = "-"  # Chunk being received/sent
        self.status["File"] = "-"   # File name being received/sent
        self.status["PSizeS"] = "-" # Packet Size Sent
        self.status["PSizeR"] = "-" # Packet Size Received
        self.status["Retransmission"] = 0   # Number of retransmissions
        self.status["TimePS"] = "-"    # Time to send packet
        self.status["TimePR"] = "-"    # Waiting time for response
        self.status["TimeBtw"] = "-"  # Time between reply
        self.status["CorruptedPackets"] = 0  # Number of corrupted packets

        self.session_store = None
        self.aead = None
        self.static_priv = None     # the responder's long-lived ECDH key (built lazily)
        self._hs_state = None       # the initiator's ephemeral key, held between handshake rounds

        # Long-term identity (secure): the fingerprint of this key is the device_id. Distinct
        # from the per-session ephemeral used in the ECDH: the ephemeral rotates every session,
        # so it can't be a stable identity. Loaded on demand (never in open mode).
        self.identity_priv = None
        self.identity_pub = None
        self.device_id = None

        if self.security_mode == 'secure':
            self._enable_secure()

        # The 1-byte sid is identity-derived by default, config-overridable. Resolved here (not
        # in open_backup) because it needs the MAC (open) or the device_id (secure), both known
        # only after config_connector / _enable_secure.
        self.session_id = self._resolve_session_id()

        gc.enable()

        # The role dispatch: "source" runs the serve loop, "collector" the drive loop.
        # current_role is RAM-only on purpose: a reboot must land on home_role.
        self.home_role = home_role
        self.current_role = home_role

        # --- serve-side state (the node as data holder) --------------------------------
        max_chunk_size = self.calculate_max_chunk_size()
        if self.chunk_size > max_chunk_size:
            self.chunk_size = max_chunk_size
            if self.debug:
                print("Chunk size too big, setting to max: ", self.chunk_size)
        self._publish_frame_size()
        self.file = None

        # The serve side's input boundary, the mirror of data_sink: with a datasource
        # attached, the serve loop pumps it (non-blocking) and installs its next pending
        # file whenever the node is idle. Without one, files arrive via set_file as ever.
        self.datasource = datasource
        self._datasource_ready = False
        # Whether the file currently installed came from that boundary, which decides
        # whether finishing with it is also the datasource's business. A file handed in
        # by set_file belongs to the caller and the queue is never told about it.
        self._file_from_datasource = False

        # --- drive-side state (the node as poller/reassembler) -------------------------
        self.debug_hops = debug_hops

        # The inter-request sleep controller lives in Pacing (policy on the logic-holder,
        # mirroring the adaptive-window extraction). Pacing is fed the sf/bw-derived bounds;
        # the init cap is the caller's `max_sleep_time`, not the ToA-derived max, preserving
        # the original two-step init, where calculate_sleep_time_bounds' max was overwritten.
        min_sleep, _ = self.calculate_sleep_time_bounds()
        self.pacing = Pacing(successful_interactions_required=successful_interactions_required,
                             max_failures=3, exponential_backoff_threshold=0.5)
        self.pacing.set_sleep_bounds(min_sleep, max_sleep_time)

        self.result_path = "Results"
        if self.config:
            self.result_path = self.config.get('result_path', "Results")
            if self.debug:
                print("Result path: ", self.result_path)

        # The completed-file output boundary, symmetric to DataSource on the serve side. The
        # default (persist to Results/<source>/ exactly as before) is built lazily on first
        # drive, so a node that only ever serves never touches the filesystem for it. On an
        # Edge this sink is where a *downlink* lands. Its drive loop only ever pulls from
        # its Hub.
        self.data_sink = data_sink
        self._drive_ready = False

        self.status["SMAC"] = "-"   # the peer's label while driving (key kept for subscribers)
        self.source_mac = None
        self.time_request = time()
        self._wire_chunk_size = None   # v3: sender's chunk_size, read from typed METADATA
        self._wire_total_len = None    # v3: sender's exact byte count, from the same METADATA

        # While serving a delegated downlink, requests arrive under the *session's* sid
        # (the Edge's), not this node's own; is_for_me accepts that one sid for the duration.
        self._delegated_sid = None

        # Any evidence the peer answered during the current drive visit, whether as a reply to
        # a poll or as a delegated pull that ran to completion. Kept separate from the
        # RF-config probe's `heard`, which asks the narrower question of whether the peer was
        # located on the config this visit polled on.
        self._peer_alive_this_visit = False

        # Hub-authority tie-break bookkeeping: the kind of the last frame that landed in the
        # reply slot, and how often this node yielded a delegated drive to the authority.
        self._last_reply_kind = None
        self.yield_count = 0

        # A control action deferred by a downlink sink (an RF-config switch or a reset). It must
        # not run inside consume(): that fires before the transfer's final-OK reaches the air, so
        # switching the radio or resetting there would break the acknowledgement. The Edge drains
        # it after the pull completes (mirrors the serve path: reply on the old config, then switch).
        self._pending_control = None

    def _enable_secure(self):
        # Custody of secure Sessions (per-peer, keyed by sid) + the per-frame AEAD backend.
        # Imported lazily so an open-mode node never pulls in the crypto modules.
        from AlLoRa.Security.Session_store import RAM_session_store
        from AlLoRa.Security.AEAD import detect_aead
        self.session_store = RAM_session_store()
        self.aead = detect_aead()
        # A secure node always has a device_id: it is its wire identity (first-contact address
        # + sid seed), needed whether or not the sid is config-overridden.
        self._ensure_identity()
        if self.aead is not None:
            self.connector.set_secure(self.session_store.get, self.aead)
            return
        # No crypto backend. A configured-secure node must NOT silently run plaintext: the
        # operator believes the link is protected, so a silent degrade is the worst outcome.
        # It halts loudly, unless an explicit opt-in (tests / bring-up only) permits open.
        from AlLoRa.Security.AEAD import unavailable_reason
        reason = unavailable_reason()
        if self.config.get('allow_insecure_fallback', False):
            print("WARNING: secure mode requested but no AEAD backend, running OPEN (insecure),",
                  "because allow_insecure_fallback is set:", reason)
            return
        raise RuntimeError(
            "secure mode requires a crypto (AEAD) backend, none available: {}. Flash the AlLoRa "
            "firmware (native CTR), or set allow_insecure_fallback for an explicit insecure run "
            "(tests/bring-up only).".format(reason))

    def _ensure_identity(self):
        # Load (or, on first boot, generate + persist) this node's long-term identity keypair
        # and its device_id. Persisting keeps the device_id stable across reboots so the
        # operator's registration stays valid; without a configured identity_file the key is
        # RAM-only (fine for tests, but the device_id then changes each boot).
        if self.device_id is not None:
            return
        from AlLoRa.Security.identity import (load_or_create_identity, device_id_from_pubkey)
        from AlLoRa.Security.ec_p256 import generate_private_key, public_key_uncompressed
        path = self.config.get('identity_file', None)
        if path:
            self.identity_priv, self.identity_pub, self.device_id = \
                load_or_create_identity(path, urandom)
        else:
            self.identity_priv = generate_private_key(urandom)
            self.identity_pub = public_key_uncompressed(self.identity_priv)
            self.device_id = device_id_from_pubkey(self.identity_pub)
            if self.debug:
                print("no identity_file configured, using an ephemeral identity "
                      "(device_id changes each boot)")

    # A control root reaches a node as one hex line in a file, the format identity.key already
    # uses, and the two halves are told apart by length alone: 130 characters is a SEC1 public
    # key, 64 is a P-256 private scalar. Nothing else is a control root.
    _CONTROL_ROOT_PUB_HEX = 130
    _CONTROL_ROOT_PRIV_HEX = 64
    # Whether this node kind issues control artifacts or only obeys them. The Hub preset is the
    # authority and sets it; everything else verifies. It decides which half of the key a node
    # may legitimately hold, which is why it is a property of the node kind and not of config:
    # config chooses whether to provision, never what a node is allowed to be.
    _MINTS_CONTROL = False

    def _configure_control_root(self):
        """Load the control root this node was provisioned with, or None if it has none.

        Unlike the identity key, an absent file is never created here. A node that generated
        its own control root would have invented its own authority, which is the opposite of
        what provisioning one means, so the key is made once per fleet by an operator and
        copied in. Every failure below therefore halts the node instead of degrading it: the
        config having named a control root, coming up without one would leave the node
        accepting unsigned commands while its configuration says it does not.
        """
        path = self.config.get('control_root_file', None)
        if not path:
            return None
        try:
            with open(path, "r") as f:
                material = f.read().strip()
        except OSError as e:
            raise ValueError(
                "control_root_file '{}' could not be read ({}). It is provisioned by the "
                "operator and never generated on the node: a node that minted its own control "
                "root would be its own authority.".format(path, e))
        return self._control_root_from(material, path)

    def _control_root_from(self, material, path):
        # The contents decide the role, and a role this node kind cannot perform is a
        # provisioning error rather than something to work around. Both mismatches are refused,
        # for different reasons: a private scalar on a node that only verifies is the fleet's
        # signing key sitting on a field node, and carrying on would hide a key compromise
        # behind a working link; a public key on the authority cannot sign anything, so the Hub
        # would fall back to sending unsigned commands while its config claims a signed
        # deployment. Almost always both are the wrong half of the pair copied in.
        if len(material) == self._CONTROL_ROOT_PRIV_HEX:
            if not self._MINTS_CONTROL:
                raise ValueError(
                    "control_root_file '{}' holds a private scalar: that is the key the whole "
                    "fleet's commands are signed with, and it does not belong on a node that "
                    "only verifies them. Provision the public key (130 hex characters) "
                    "instead.".format(path))
            from AlLoRa.Control.Control_Root import Control_Root
            return Control_Root(material)
        if len(material) == self._CONTROL_ROOT_PUB_HEX:
            if self._MINTS_CONTROL:
                raise ValueError(
                    "control_root_file '{}' holds only the public half: this node mints the "
                    "commands it sends and cannot do so with a verifying key. Provision the "
                    "private scalar (64 hex characters), or provision nothing here if the "
                    "artifacts are minted elsewhere and handed to ask_change_rf.".format(path))
            from AlLoRa.Security.ec_p256 import decode_public_key
            try:
                key = bytes.fromhex(material)
            except ValueError:
                raise ValueError(
                    "control_root_file '{}' is not hexadecimal".format(path))
            decode_public_key(key)   # raises on an off-curve or malformed key
            return key
        raise ValueError(
            "control_root_file '{}' holds {} characters: a control root is either a SEC1 "
            "public key ({} hex characters) or a P-256 private scalar ({}), in the same hex "
            "format identity.key uses.".format(path, len(material),
                                               self._CONTROL_ROOT_PUB_HEX,
                                               self._CONTROL_ROOT_PRIV_HEX))

    def _resolve_session_id(self):
        # The 1-byte session address. An explicit config value always wins (debugging, or the
        # Collector's on-clash reassignment). Otherwise it is identity-derived: device_id[0] in
        # secure, the device-specific low byte of the short MAC in open (never the OUI bytes).
        explicit = self.config.get('session_id', None)
        if explicit is not None:
            return explicit
        if self.security_mode == 'secure':
            self._ensure_identity()
            return self.device_id[0]
        return int(self.MAC[-2:], 16)

    # --- first-contact handshake over the wire (open device_id-addressed CTRL frames) -------
    # The exchange rides the shared request/respond verbs. Message kinds ride a 1-byte prefix
    # on the CTRL payload (provisional layout): the Hub (responder + sid-assigner) drives
    # two rounds, the Edge (initiator) answers with its ephemeral key then completes. Frames
    # are addressed by the Edge's device_id[:4] (no MAC on the wire); a MAC-registered peer
    # keeps the retiring two-MAC shape.
    _HS_INIT = 0        # Hub -> Edge: begin (prompt for the ephemeral key)
    _HS_HELLO = 1       # Edge -> Hub: ephemeral public key
    _HS_WELCOME = 2     # Hub -> Edge: static public key + assigned sid
    _HS_ACK = 3         # Edge -> Hub: session established

    def _ctrl_packet(self, token, hs_kind, payload=b"", addressing="did"):
        # A first-contact v3 CTRL frame (no sid until the handshake assigns one); the hybrid
        # codec puts it on the wire open. Addressed by device_id[:4] (v3, no MAC on the wire),
        # a single token stamped the same in both directions, or, for a MAC-registered peer,
        # by the retiring two-MAC shape.
        p = Packet_v3(mesh_mode=self.mesh_mode, addressing=addressing)
        if addressing == "did":
            p.set_did(token)
        else:
            p.set_source(self.MAC)
            p.set_destination(token)
        p.set_kind(Packet_v3.CTRL)
        p.set_payload(bytes([hs_kind]) + payload)
        return p


    def open_backup(self):
        with open(self.config_file, "r") as f:
            self.config = loads(f.read())

        self.name = self.config.get('name', "N")
        self.debug = self.config.get('debug', False)
        self.mesh_mode = self.config.get('mesh_mode', False)
        self.short_mac = self.config.get('short_mac', False)
        self.chunk_size = self.config.get('chunk_size', 235)

        # v3: protocol version + open-mode session addressing.
        # Defaults keep v2 behavior byte-for-byte (version 2, MAC addressing).
        self.protocol_version = self.config.get('protocol_version', 2)
        self.security_mode = self.config.get('security_mode', 'open')
        if self.security_mode == 'strict':
            # 'strict' is a designed posture (the Edge authenticates the Hub, not just the
            # other way round) whose crypto is not built yet. It has to be refused here rather
            # than passed on, because the half of it that does exist is the dangerous half: the
            # sid derives from the identity, so the node looks registered and secure on the
            # wire, while the frames it sends are plaintext. Refusing at startup is the whole
            # point; a posture that silently delivers less than it names is worse than no
            # posture at all.
            raise ValueError(
                "security_mode 'strict' is not implemented yet: it would derive an "
                "identity-based session id and then send plaintext. Use 'secure' for "
                "authenticated, encrypted frames, or 'open' for none.")
        # session_id is resolved after init (identity-derived unless config overrides); see
        # _resolve_session_id. It is read from config here only as the explicit override source.
        self.addressing = 'sid' if self.protocol_version >= 3 else 'mac'

        self.config_connector_dic = self.config.get('connector', None)    #{"freq" : lora_config['freq'], "sf": lora_config['sf']}
        self.config_connector_dic['mesh_mode'] = self.mesh_mode
        self.config_connector_dic['short_mac'] = self.short_mac
        self.config_connector_dic['protocol_version'] = self.protocol_version
        self.config_connector_dic['addressing'] = self.addressing

        if self.debug:
            print(self.config)

    def _commit_json(self, path, content):
        """Write a config file all at once, or not at all.

        An Edge that loses LoRa.json is off its own network and a Hub that loses Nodes.json
        has no fleet left to poll, so neither file is ever written under the name the node
        boots from. See `commit_file`.
        """
        commit_file(path, dumps(content))

    def backup_config(self):
        # Lossless round-trip: re-read the persisted config and overlay ONLY what changes at
        # runtime (the chunk size and the live RF params), then write the whole dict back.
        # Rebuilding a hand-picked subset instead dropped protocol_version / security_mode /
        # session_id / identity_file / result_path — so a registered secure node that
        # committed an RF-config change came back on reboot as an open v2 node, off its own
        # network. The live RF values come from the connector (change_rf_config moved the
        # radio without touching the on-disk values), written under their canonical keys.
        with open(self.config_file, "r") as f:
            conf = loads(f.read())
        conf["chunk_size"] = self.chunk_size
        freq, sf, bw, cr, tx_power = self.connector.get_rf_config()
        connector = conf.get("connector", {})
        connector["freq"] = freq
        connector["sf"] = sf
        connector["bandwidth"] = bw
        connector["coding_rate"] = cr
        connector["tx_power"] = tx_power
        conf["connector"] = connector
        self._commit_json(self.config_file, conf)

    def config_connector(self):
        self.connector.config(self.config_connector_dic)

        self.MAC = self.connector.get_mac()[-8:]
        self.status["MAC"] = self.MAC
        print(self.name, ":", self.MAC)

    def get_mesh_mode(self):
        return self.mesh_mode

    def new_packet(self):
        """Build an outgoing packet for the negotiated protocol version.

        v3 frames are session-id-addressed (the sid is pre-set here); v2 frames keep
        MAC addressing. Callers that need MAC fields (v2) set source/destination after.
        """
        if self.protocol_version >= 3:
            packet = Packet_v3(mesh_mode=self.mesh_mode, addressing=self.addressing)
            packet.set_session(self.session_id)
            return packet
        return Packet(self.mesh_mode, self.short_mac)

    def is_for_me(self, packet):
        if self.protocol_version >= 3:
            if packet.addressing == "did":   # v3 first contact: addressed to my device_id[:4]
                return self.device_id is not None and packet.get_did() == self.device_id[:4]
            if packet.addressing == "mac":   # legacy first-contact / handshake frame (no sid yet)
                return packet.get_destination() == self.MAC
            if packet.get_session() == self.session_id:
                return True
            # While serving a delegated downlink, requests arrive under the *session's* sid
            # (the Edge's), not this node's own; accept that one sid for the duration.
            return (self._delegated_sid is not None
                    and packet.addressing == "sid"
                    and packet.get_session() == self._delegated_sid)
        return packet.get_destination() == self.MAC

    # --- the shared one-round engine: request (initiator) / respond (responder) ------------
    # Both roles run the same round from opposite sides, so both verbs live here on the Node.
    # Role reversal is just a node calling the other verb (the drive swaps; the type and
    # the trust-anchor don't).

    def request(self, packet):
        """One initiator round: transmit a request and wait for its reply. Returns the
        connector's (response | error dict, size_sent, size_recv, td) tuple. Driven by
        a Hub to pull a transfer; a role-reversed Edge runs it too."""
        return self.connector.send_and_wait_response(packet)

    def respond(self, handler):
        """One responder round: receive a request and, if it is addressed to me, hand it to
        `handler` (which produces and sends the reply); otherwise forward it (mesh relay).
        Returns the received request packet, or None if nothing arrived, so a role-specific
        driver keeps its own bookkeeping (sf-trial, timeout)."""
        packet = self.listen_requester()
        if packet is None:
            return None
        if self.is_for_me(packet):
            handler(packet)
        else:
            self.forward(packet)
        return packet

    # Receive one request and parse it, via the same connector.listen + codec.deframe seam the
    # initiator's send_and_wait_response uses (this is the de-dup: the responder no longer
    # hand-rolls recv + Packet.load).
    def listen_requester(self):
        focus_time = self.connector.adaptive_timeout
        data, td = self.connector.listen(focus_time)   # one timed window; td measured at the radio
        self.tr = time()   # when the request landed (send_response times the reply from here)

        if not data:
            if self.debug:
                print("No data received within focus time")

            self.connector.increase_adaptive_timeout()
            return None

        packet = self.connector.codec.deframe(data)
        if packet is None:
            # A frame we can't parse. deframe is safe (never throws), so like the initiator's
            # recv path we just treat it as no usable request and wait again.
            if self.debug:
                print("Could not parse frame: ", data)
            return None

        if self.mesh_mode:
            try:
                packet_id = packet.get_id()  # Check if already forwarded or sent by myself
                if packet_id in self.LAST_SEEN_IDS or packet_id in self.LAST_IDS:
                    if self.debug:
                        print("ALREADY_SEEN", self.LAST_SEEN_IDS)
                    return None
            except Exception as e:
                if self.debug:
                    print(e)

        if self.debug:
            rssi = self.connector.get_rssi()
            snr = self.connector.get_snr()
            print('LISTEN_REQUESTER({}) at: {} || request_content : {}'.format(td, self.connector.adaptive_timeout, packet.get_content()))
            print("RSSI: ", rssi, " SNR: ", snr)
            self.status['RSSI'] = rssi
            self.status['SNR'] = snr
            self.status['PSizeR'] = len(data)
            self.status['TimePR'] = td * 1000  # Time in ms

        self.connector.decrease_adaptive_timeout(td)

        return packet

    def send_response(self, response_packet: Packet):
        if response_packet:
            if self.mesh_mode:
                response_packet.set_id(self.generate_id())
            t0 = time()
            if self.connector.sf == 12:
                sleep(1)
            self.send_lora(response_packet)
            tf = time()
            time_send = tf - t0
            time_reply = tf - self.tr
            if self.debug:
                print("Time Send: ", time_send, " Time Reply: ", time_reply)
            if self.status.subscribers:
                self.status['PSizeS'] = len(response_packet.get_content())
                self.status['TimePS'] = time_send
                self.status['TimeBtw'] = time_reply
                self.status.notify()

    def forward(self, packet: Packet):
        try:
            if packet.get_mesh():
                if self.debug:
                    print("FORWARDED", packet.get_content())

                random_sleep = 0
                if packet.get_sleep():
                    random_sleep = (urandom(1)[0] % 5 + 1) * 0.1

                if packet.get_debug_hops():
                    packet.add_hop(self.name, self.connector.get_rssi(), random_sleep)
                packet.enable_hop()
                if random_sleep:
                    sleep(random_sleep)

                success = self.send_lora(packet)
                if success:
                    self.LAST_SEEN_IDS.append(packet.get_id())
                    self.LAST_SEEN_IDS = self.LAST_SEEN_IDS[-self.MAX_IDS_CACHED:]
                else:
                    if self.debug:
                        print("ALREADY_FORWARDED", self.LAST_SEEN_IDS)
        except Exception as e:
            # If packet was corrupted along the way, won't read the COMMAND part
            if self.debug:
                print("ERROR FORWARDING", e)

    def generate_id(self):
        id = -1
        while (id in self.LAST_IDS) or (id == -1):
            id = int.from_bytes(urandom(2), 'little')
        self.LAST_IDS.append(id)
        self.LAST_IDS = self.LAST_IDS[-self.MAX_IDS_CACHED:]
        return id

    def check_id_list(self, id):
        if id in self.LAST_SEEN_IDS:
            return False
        self.LAST_SEEN_IDS.append(id)
        self.LAST_SEEN_IDS = self.LAST_SEEN_IDS[-self.MAX_IDS_CACHED:]
        return True

    def send_lora(self, packet):
        return self.connector.send(packet)

    def change_rf_config(self, new_config):
        print("Changing RF Config to: ", new_config)
        frequency = new_config.get("freq", None)
        sf = new_config.get("sf", None)
        bw = new_config.get("bw", None)
        cr = new_config.get("cr", None)
        tx_power = new_config.get("tx_power", None)
        chunk_size = new_config.get("cks", None)
        if self.debug:
            print("Changing RF Config to: ", frequency, sf, bw, cr, tx_power, chunk_size)
        changed = self.connector.change_rf_config(frequency=frequency,
                                        sf=sf, bw=bw, cr=cr,
                                        tx_power=tx_power)
        if not changed:
            # The radio refused: the connector already restored every parameter it had
            # touched, so nothing above it may move either. A chunk size applied here after a
            # rolled-back change would chunk for a config the node is not on, and the serve
            # loop re-chunks an in-flight file whenever the chunk size moves, so a *failed*
            # change would disturb a transfer that was proceeding fine. A failed change is a
            # no-op, all of it or none of it.
            return False

        if chunk_size:
            self.chunk_size = chunk_size

        max_chunk_size = self.calculate_max_chunk_size()
        if self.chunk_size > max_chunk_size:
            self.chunk_size = max_chunk_size
            print("Chunk size too big, changing to: ", self.chunk_size)
        self._publish_frame_size()

        # Arm the trial. The window rides the (signed) payload as `trial` seconds; absent,
        # a ToA-scaled default sized off the new config's receive window. Both deadlines are
        # armed lazily on the loop's first service (so they count from running on the new
        # config), so only clear them here.
        self.sf_trial = True
        self._trial_window_s = new_config.get("trial", None)
        self._trial_deadline = None
        self._trial_ceiling = None
        self.status["Freq"] = self.connector.frequency
        self.status["SF"] = self.connector.sf
        self.status["BW"] = self.connector.bw
        self.status["CR"] = self.connector.cr
        self.status["TX_P"] = self.connector.tx_power
        return True

    def _publish_frame_size(self):
        # The connector sizes its receive window from the biggest frame it will really see.
        # Only this node knows that number: it is the clamped chunk plus the codec's
        # overhead, and the codec is asked here for the same reason it is asked below.
        self.connector.set_frame_size(self.chunk_size + self.connector.codec.payload_overhead())

    def calculate_max_chunk_size(self):
        # How many payload bytes fit beside the framing, for whatever this node actually
        # speaks. The cost differs a lot by version and posture (v2 spends 12 bytes on two
        # MAC addresses; v3 addresses by session id in 6; secure trades the integrity trailer
        # for a sealed header and a tag, 8), so the codec is asked rather than assumed. It was
        # previously hardcoded to the v2 header, which silently held every v3 node at v2's
        # ceiling and made the whole point of session-id addressing unreachable.
        #
        # Safe to read here: the secure codec is installed during Node.__init__, before any
        # caller of this method runs, and a node that fell back to open reports the open cost,
        # which is what it will really put on the wire.
        return (self.connector.get_max_payload_size()
                - self.connector.codec.payload_overhead())

    def restore_rf_config(self):
        self.connector.restore_rf_config()

    # Subscribers stuff: the surface itself lives on `status`; these stay because they are
    # what a deployment's main.py calls (a screen, a logger, a benchmark probe).
    def register_subscriber(self, subscriber):
        self.status.register(subscriber)

    def unregister_subscriber(self, subscriber):
        self.status.unregister(subscriber)

    def notify_subscribers(self):
        self.status.notify()

    def _prepare_drive(self):
        # First-drive setup: the Results dir must exist before a reassembly buffer opens its
        # temp file under it, and a node with no injected sink gets the disk default.
        if self._drive_ready:
            return
        try:
            os.mkdir(self.result_path)
        except Exception as e:
            if self.debug:
                print("Error creating result path: {}".format(e))
        if self.data_sink is None:
            from AlLoRa.DataSinks.Disk_DataSink import Disk_DataSink
            self.data_sink = Disk_DataSink(self.result_path)
        self._drive_ready = True

    # `NEXT_ACTION_TIME_SLEEP` now lives in `Pacing.sleep`; this property keeps every call
    # site working (the loop's `finally`, `Hub.run`, examples).
    @property
    def NEXT_ACTION_TIME_SLEEP(self):
        return self.pacing.sleep

    @NEXT_ACTION_TIME_SLEEP.setter
    def NEXT_ACTION_TIME_SLEEP(self, value):
        self.pacing.sleep = value

    # =====================================================================================
    # Serve side. The responder loop: hold a file, answer each request for it.
    # =====================================================================================

    def get_chunk_size(self):
        return self.chunk_size

    def got_file(self):     # Check if I have a file to send
        return self.file is not None

    def _refuse_oversized_chunks(self, file: AlLoRa_File):
        """Raise if this file states chunks bigger than this node's frames can carry.

        The one number that knows is `self.chunk_size`, clamped by
        `calculate_max_chunk_size()` against what the codec spends on framing. A file cut
        above it does not fail at the door: it is encoded, sent, and either truncated or
        rejected somewhere further down, which is how one 32 KiB secure arm was lost on
        2026-08-11 to chunks that were 2 bytes too wide for a sealed frame.

        Refusing rather than re-cutting is deliberate. Silently substituting our own number
        would leave the caller with a value it can neither see nor trust, and a caller that
        states a size usually states it somewhere else too: the record length it writes, the
        buffer it sized, the count it expects at the far end.
        """
        if file.chunk_size is not None and file.chunk_size > self.chunk_size:
            raise ValueError(
                "{} is cut into {}-byte chunks, more than the {} bytes this node can carry "
                "beside its framing. Leave the chunk size out and the node cuts the file "
                "itself, or lower it to {} or less.".format(
                    file.get_name(), file.chunk_size, self.chunk_size, self.chunk_size))

    def set_file(self, file: AlLoRa_File):
        # A file handed in by a caller, on the caller's terms: a chunk size it stated is
        # kept, one it left out is this node's to supply, and one too wide for the frame is
        # refused rather than quietly narrowed. Refused *before* anything is written to the
        # file, because a rejected call must not leave the object it rejected altered.
        self._refuse_oversized_chunks(file)
        if file.chunk_size is None:
            file.change_chunk_size(self.chunk_size)
        # Installing a file to serve starts a fresh delivery, even if this same
        # object was already served once (re-queued downlink, broadcast, or a queued
        # file whose first attempt did not finish).
        file.reset_delivery()
        self.file = file
        # A caller handing in its own file owns it. _pump_datasource sets this back
        # after calling us, so the queue is only credited for what the queue supplied.
        self._file_from_datasource = False

    def _serve_from_boundary(self, file: AlLoRa_File):
        """Install a file taken off an input boundary, cut to this node's current size.

        A boundary stores bytes under a name and states no chunk size, so cutting the file
        is this node's job and it is done on every install rather than once when the
        boundary was attached. That matters twice over: a signed RF_CONFIG can carry a new
        `cks` and this node re-clamps it, and a file whose delivery did not complete stays
        queued and comes back later, by which time the radio may have moved. Re-cutting is
        safe exactly here, between files: a boundary is only ever asked for one while this
        node holds none, so no collector is mid-way through assembling what is being cut.
        """
        file.change_chunk_size(self.chunk_size)
        self.set_file(file)

    def restore_file(self, file: AlLoRa_File):
        """v2 only. Put a file back mid-flight, announced, so a transfer carries on.

        The caller's word is what makes this safe, and in v2 it was earned a line earlier:
        `establish_connection` had just heard the peer ask for something other than a
        connection poll, which is the peer saying a transfer is already open. v3 retired
        that step, so the same call here would assert an announcement nobody made, and the
        chunks served on the back of it would come out of whatever file this node is
        holding now: honest frames, a saved file spliced from two.

        A v3 node resumes by asking its input boundary instead. Only a DataSource that
        keeps its queue on flash can promise the file it hands back after a reboot is the
        one that was being sent, and only then does the serve loop install it as announced.
        """
        if self.protocol_version >= 3:
            # Refused at the call, where the mistake is. Left unguarded it would surface as
            # a corrupt file at the far end, days later, with nothing pointing back here.
            raise NotImplementedError(
                "restore_file() is v2 only: it installs a file as already announced, which "
                "in v2 was proven by establish_connection first. A v3 node resumes across a "
                "restart by serving from a DataSource that vouches for its queue "
                "(is_durable), and restarts the transfer when none does.")
        self.set_file(file)
        self.file.first_sent = time()
        self.file.metadata_sent = True

    def _pump_datasource(self):
        # One cooperative round of the input boundary, from inside the serve loop: it
        # shares the radio loop, so check() must never block.
        #
        # The head is borrowed, not taken: it stays queued until _retire_file says it
        # was delivered. A queue that only ever lived in RAM lost nothing by handing the
        # file over at the start, since a reboot took the rest of the queue with it. A
        # queue backed by flash does not work that way, and dropping the file here would
        # erase it before a single chunk of it reached the air. Serving from the head is
        # also how the same node already serves a downlink it was delegated, so both
        # directions retire a queued file on the same evidence: the peer confirmed it.
        if self.datasource is None:
            return
        if not self._datasource_ready:
            self.datasource.prepare()
            self._datasource_ready = True
        self.datasource.check()
        if self.file is None and self.datasource.has_pending():
            file = self.datasource.peek_file()
            self._serve_from_boundary(file)
            if self.datasource.is_durable():
                # The queue vouches that this name is still the same bytes, so a transfer
                # the reboot interrupted can be continued rather than started over: the
                # collector drives, and it asks for its own missing indexes. Installing the
                # file as already announced is what allows that, and it is safe only here,
                # where a boundary that keeps its files on flash has said so. From a queue
                # that lost the file it would be a lie, and the chunks served on the back of
                # it would come out of whatever took its place.
                #
                # It costs nothing when no transfer was interrupted: a collector opening a
                # fresh one asks for METADATA anyway, and gets it.
                file.metadata_sent = True
            self._file_from_datasource = True

    def _retire_file(self, delivered):
        """Finish with the installed file, and tell the queue whether it made it.

        `delivered` is the peer's confirmation, not this node's opinion: only a transfer
        the far end acknowledged drops the file off the queue. Anything else (a timeout,
        a partial send, a link that died mid-file) leaves it queued to be served again,
        which is the at-least-once the durable queue exists to provide. The cost of that
        choice is a duplicate whenever a confirmation is the thing that got lost, and a
        duplicate is recoverable where a missing reading is not.
        """
        if self._file_from_datasource:
            self._file_from_datasource = False
            if delivered and self.datasource is not None:
                self.datasource.confirm_file()
        self.file = None

    def establish_connection(self, try_for=None):
        """v2 only. Wait for a peer and optionally agree an RF change before transferring.

        v3 has no separate connect step: serve a file and let `send_file()` block until the
        peer asks for it. That synchronizes the two ends exactly as this did, and it also
        covers the secure handshake, which this does not.
        """
        if self.protocol_version >= 3:
            # This negotiates over v2 RF-change verbs the v3 frame does not carry, so left
            # unguarded it raises AttributeError on the first packet received: deep inside
            # the receive loop, after the radio is already running, and reading as a library
            # bug rather than a wrong call. Refuse at the call instead, where the mistake is.
            raise NotImplementedError(
                "establish_connection() is v2 only: it negotiates RF changes with frame "
                "fields that do not exist in version 3. A v3 node waits for its peer by "
                "serving a file and letting send_file() block until the peer asks for it.")
        while True:
            if self.debug:
                print("Establish")
            new_sf = None
            packet = self.listen_requester()
            if packet:
                if self.is_for_me(packet):
                    command = packet.get_command()
                    if Packet.check_command(command):
                        if command != Packet.OK:
                            return True
                        response_packet = Packet(self.mesh_mode, self.short_mac)
                        response_packet.set_source(self.MAC)
                        response_packet.set_destination(packet.get_source())
                        response_packet.set_ok()

                        if packet.get_change_rf():
                            new_sf = packet.get_config()
                            response_packet.set_change_rf(new_sf)
                        if self.mesh_mode and packet.get_mesh() and packet.get_hop():
                            response_packet.enable_mesh()
                            if not packet.get_sleep():
                                response_packet.disable_sleep()
                        if packet.get_debug_hops():
                            response_packet.add_previous_hops(packet.get_message_path())
                            response_packet.add_hop(self.name, self.connector.get_rssi(), 0)

                        self.send_response(response_packet)

                        if self.status.subscribers:
                            self.status['Status'] = 'OK'
                            self.status.notify()

                        if new_sf:
                            response_packet.set_change_rf(new_sf)
                            self.change_rf_config(new_sf)

                        return False
                else:
                    if self.debug:
                        print("Not for me, my mac is: ", self.MAC, " and packet mac is: ", packet.get_destination())
                    self.forward(packet)
            gc.collect()
            if try_for is not None:
                try_for -= 1
                if try_for <= 0:
                    return False

    def answer_handshake(self, request):
        """As the serve side (handshake initiator), answer the drive side's handshake CTRL: on
        INIT, make a fresh ephemeral key and return the HELLO (its public key); on WELCOME,
        derive + store the session and return the ACK. Returns the reply packet, or None on an
        unexpected message. A fresh ephemeral key per session means a reboot re-handshakes."""
        from AlLoRa.Security.handshake import initiator_hello, initiator_complete
        payload = request.get_payload()
        # Answer in the same addressing the poll used: under my own device_id[:4] (v3), or
        # mirrored back to the poller's MAC (the retiring legacy shape).
        if request.addressing == "did":
            addressing, token = "did", self.device_id[:4]
        else:
            addressing, token = "mac", request.get_source()
        if payload and payload[0] == Node._HS_INIT:
            # A repeated INIT (our HELLO was lost) just makes a fresh ephemeral. The latest
            # one is what the poller will accept, so the two ends stay in step.
            self._hs_state, hello = initiator_hello(urandom)
            return self._ctrl_packet(token, Node._HS_HELLO, hello, addressing=addressing)
        if payload and payload[0] == Node._HS_WELCOME:
            if self._hs_state is not None:
                # If the WELCOME shed its sid (the common case), fall back to the sid both ends
                # derive from my identity, device_id[0].
                session = initiator_complete(self._hs_state, payload[1:],
                                             default_sid=self.device_id[0])
                self.session_store.put(session)
                # Follow the established session's sid into the data phase: the peer may
                # have reassigned it off device_id[0] to break a clash, and my data frames must
                # carry the same sid the session was keyed under.
                self.session_id = session.sid
                self._hs_state = None
            # If the state is already cleared, a prior WELCOME completed and its ACK was lost;
            # re-ACK idempotently so the peer's retransmit still lands.
            return self._ctrl_packet(token, Node._HS_ACK, addressing=addressing)
        return None

    def _handshake_responder(self, request):
        # respond()-compatible handler: build the handshake reply and send it.
        self.send_response(self.answer_handshake(request))

    def _respond_handler(self, packet):
        # During a secure transfer the serve side may still get a first-contact handshake CTRL
        # (e.g. the peer re-handshaking); route those to the handshake, data to serving.
        kind = packet.get_command()
        if kind == Packet_v3.GRANT:
            # A delegation from the authority. Never answered on the wire: the granted pull
            # itself is the acknowledgement, and only an Edge preset honors it.
            self._on_grant(packet)
            return
        if kind == Packet_v3.CTRL:
            # Two vocabularies share the CTRL kind, told apart by bit 7 of the payload's first
            # byte: handshake kinds below it, control types at and above. Splitting them here
            # is what lets an unrecognised frame be dropped *as* the thing it claimed to be;
            # before, an unknown handshake kind and an unknown control type were the same
            # silent None and neither could be logged for what it was.
            payload = packet.get_payload()
            if payload and (payload[0] & IN_BAND):
                self._handle_in_band_control(packet, payload)
            else:
                self.send_response(self.answer_handshake(packet))
        else:
            self._serve(packet)

    def _handle_in_band_control(self, packet, payload):
        """Act on a control command that arrived on the link itself, with nothing wrapping it.

        The command is unauthenticated by construction: an open link authenticates nothing, and
        a node in radio range of an attacker can already be disrupted worse than by a retune.
        What must hold is that whether this tier is accepted at all is a property of the node,
        not of the frame, which is why the two refusals below are unconditional rather than
        configurable. One is bound to what this node is, the other to how it was provisioned.
        """
        # One mask picks the namespace (done by the caller), one reads the type back verbatim.
        # Masked rather than subtracted so a reserved bit that later gains a meaning lands
        # outside the closed enum and is dropped, instead of aliasing onto a real type.
        control_type = payload[0] & 0x7F
        if self.home_role == "collector":
            # Actuation follows authority: an authority evaluates control from below, it does
            # not apply it. Mechanically a peer holding the collector role can drive a control
            # round, since driving is what retunes RF, but a delegated role carries no
            # authority with it. And the harm is not local: a wrongly retuned Edge costs that
            # node until its trial reverts it, while a wrongly retuned authority moves the
            # aggregation point for every node aimed at it.
            if self.debug:
                print("Ignoring an in-band control command: this node is the authority")
            return
        if self.control_root is not None:
            # Provisioning a control root is the operator declaring an external authority over
            # this node, and a node that has the stronger tier accepts nothing below it. If an
            # unsigned frame could still retune it, the signed path would secure nothing: an
            # attacker in radio range would ask in band rather than forge a signature it cannot
            # produce. Checked after the authority test only so a minting Hub, which is refused
            # on both counts, reports the reason that holds even when it mints for no one.
            if self.debug:
                print("Refusing an in-band control command: this node verifies signed control")
            return
        if control_type not in CONTROL_TYPES:
            if self.debug:
                print("Dropping an in-band control command of unknown type: ", control_type)
            return
        if self.control_actuator is None:
            # Nothing to act with. Staying silent rather than acknowledging is the point: an
            # ack would move the commanding end onto a configuration this node will never
            # apply, which is a deaf endpoint rather than a failed command.
            if self.debug:
                print("Dropping an in-band control command: no actuator on this node")
            return
        self.control_actuator.apply(control_type, bytes(payload[1:]))
        ack = self.new_packet()
        if packet.addressing == "sid":
            ack.set_session(packet.get_session())
        ack.set_kind(Packet_v3.CTRL)
        # The prefix alone, no body: the commanding end already holds what it asked for, and
        # the type acked is what proves this node parsed the type that was sent.
        ack.set_payload(bytes([payload[0]]))
        self.send_response(ack)
        # The safe boundary the actuator defers to: the acknowledgement is on the air, so
        # switching the radio (or resetting) can no longer cost the peer its confirmation.
        self._run_pending_control()

    def _on_grant(self, packet):
        # Base: ignore. The Edge preset overrides this to accept the delegated collector role;
        # a Hub (the authority) never takes a GRANT from anyone.
        pass

    def _service_grant(self):
        # Base: nothing to honor. The Edge preset overrides this to run a pending
        # delegated pull. Called from every serve wait loop (`serve()` AND the legacy
        # `send_file()` main loops fielded firmware runs), so a delegation reaches an
        # Edge no matter which loop it lives in.
        pass

    def _maybe_delegate(self, digital_endpoint):
        # Base: nothing to delegate. The Hub preset overrides this to hand the collector role
        # to an Edge (GRANT) when a downlink file is pending for it at a safe boundary.
        return False

    def _probe_visit_end(self, digital_endpoint, heard, completed):
        # Base: nothing to probe. The Hub preset overrides this to advance a {new, old}
        # RF-config trial once per visit: locate/commit the Edge on the config it answered,
        # or swap the probe to the other config when a visit heard nothing.
        pass

    def _session_visit_end(self, digital_endpoint):
        # Base: nothing to check. The Hub preset overrides this to tear down a secure session
        # its endpoint has stopped being able to use, so the next visit re-handshakes. Reads
        # `_peer_alive_this_visit`, which is any evidence the peer answered during the visit.
        pass

    def queue_control_action(self, action):
        # A downlink control sink calls this from consume() to defer its actuation (a zero-arg
        # thunk) instead of running it in place. Only the latest is kept: a control artifact is
        # rare and one-at-a-time, and a superseding command should win.
        self._pending_control = action

    def _run_pending_control(self):
        # Drain a deferred control action at a safe boundary (after the final-OK is on the air).
        # Clear first, then invoke: a reset never returns, and clearing up front guarantees the
        # action can never run twice even if invoking it re-enters here.
        action = self._pending_control
        self._pending_control = None
        if action is not None:
            action()

    def _heard_authority_poll(self):
        # Hub-authority tie-break. Only a *delegated* drive (an Edge granted the collector
        # role) can hear this: during its pull the only OK-kind frame that can land in the
        # reply slot is the authority polling again: it reclaimed, so the delegation is
        # over. The Edge yields instantly and goes home; the abandoned pull simply re-runs
        # on a later GRANT with a fresh buffer. A permanent authority never yields.
        if (self.home_role == "source" and self.current_role == "collector"
                and self._last_reply_kind == Packet_v3.OK):
            self.yield_count += 1
            if self.debug:
                print("Yielding the collector role: the authority is polling again")
            return True
        return False

    def _default_trial_window(self):
        # ToA-scaled fallback when the payload carried no `trial`: a generous multiple of the
        # new config's receive window, so a slow SF gets a proportionally longer trial. The
        # backend is expected to size `trial` to exceed one Hub rotation; this only keeps a
        # window-less command from either committing on noise or restoring too eagerly.
        _, max_window = self.calculate_sleep_time_bounds()
        return max(30.0, max_window * 60)

    def _commit_trial(self):
        # The RF-config trial succeeded: a full-payload exchange completed on the new config,
        # so it becomes the last-known-good. Persist it (a reboot must land on the config that
        # works) and re-seed Pacing, whose sf/bw-derived bounds were computed on the old config.
        if not self.sf_trial:
            return
        self.sf_trial = False
        self._trial_deadline = None
        self._trial_ceiling = None
        if self.debug:
            print("RF trial committed: new config is last-known-good")
        self.backup_config()
        self.reset_sleep_time()

    def _restore_trial(self):
        # The RF-config trial failed (the new config was unreachable): fall back to the
        # last-known-good the connector snapshotted when the change was applied, and re-seed
        # Pacing on the restored config.
        if not self.sf_trial:
            return
        self.sf_trial = False
        self._trial_deadline = None
        self._trial_ceiling = None
        if self.debug:
            print("RF trial restored: reverting to last-known-good")
        self.restore_rf_config()
        self.reset_sleep_time()

    def _service_trial_window(self):
        # The self-restore backstop, called each turn of every serve/drive loop. An armed trial
        # that neither commits (a full exchange, resolved in `response`) nor is held alive by a
        # reachability poll must fall back to last-known-good once its window elapses. Silence
        # (unreachable) and a stalled transfer (requests heard, no progress) both land here. The
        # deadline is armed lazily so the window counts from the first turn on the new config.
        #
        # The ceiling is armed here too, from the same clock read so the two can never drift,
        # and is the whole trial's bound: a held window can be pushed out forever, so without
        # it a node can sit on an unproven config for as long as a peer keeps polling. On
        # expiry the node RESTORES rather than commits, even though the holds prove the peer
        # is reaching us: the commit criterion is a full payload exchange precisely because
        # short control frames landing does not prove chunks will, and a config that carries
        # polls but not payload is not a good config for a file transfer.
        if not self.sf_trial:
            return
        if self._trial_deadline is None:
            window = self._trial_window_s or self._default_trial_window()
            now = time()
            self._trial_deadline = ticks_add(now, int(window * 1000))
            self._trial_ceiling = ticks_add(
                now, int(window * self.TRIAL_CEILING_WINDOWS * 1000))
            return
        if ticks_diff(self._trial_deadline, time()) <= 0:
            self._restore_trial()
        elif self._trial_ceiling is not None and ticks_diff(self._trial_ceiling, time()) <= 0:
            if self.debug:
                print("RF trial ceiling reached: the config was held but never proven")
            self._restore_trial()

    def _hold_trial(self):
        # A short control frame (metadata / OK connection poll) heard on the new config proves
        # the peer can still reach us: hold the provisional config and push the restore deadline
        # out, rather than rolling back a reachable link that simply had no data to move.
        #
        # A stalled CHUNK loop (the same chunk asked for again and again) does not reach here,
        # so it is resolved by the window. A stalled METADATA loop DOES: the caller holds on
        # every metadata request, before it can tell a retransmission from a fresh ask, so a
        # peer whose replies keep getting lost holds the trial with every re-ask. Only the
        # ceiling bounds that, which is why the hold may extend the window but never the trial.
        if self.sf_trial and self._trial_deadline is not None:
            window = self._trial_window_s or self._default_trial_window()
            self._trial_deadline = ticks_add(time(), int(window * 1000))

    def _serve(self, packet):
        # The serve side's responder handler: build the reply for this request, send it, and
        # apply any accepted RF change (after the confirming reply is on the wire).
        response_packet, new_sf = self.response(packet)
        self.send_response(response_packet)
        if new_sf:
            backup_cks = self.chunk_size
            self.change_rf_config(new_sf)
            # Idle serving with no file is legal (an Edge between transfers): the new
            # chunk size then only lands on self.chunk_size, for the next set_file.
            if self.chunk_size != backup_cks and self.file is not None:
                self.file.change_chunk_size(self.chunk_size)

    def send_file(self, timeout=float('inf')):
        # timeout is in SECONDS, matching serve() / listen_to_endpoint() and every other v3
        # public timeout; the wrap-safe deadline math below runs in ms, so scale it here.
        t0 = time() # Start time in ms
        timeout_ms = timeout * 1000
        while not self.file.sent:
            packet = self.respond(self._respond_handler)
            self._service_grant()
            self._service_trial_window()

            if ticks_diff(time(), t0) > timeout_ms:
                last_sent = self.file.last_chunk_sent
                # Not delivered even if some chunks went out: a file the peer never
                # acknowledged stays queued rather than being counted as sent.
                self._retire_file(False)
                gc.collect()
                if self.debug:
                    print("Timeout reached")
                # If something was sent, but not all, we return a True
                # (chunk indexes are 0-based, so "chunk 0 was sent" must count as sent)
                if last_sent is not None:
                    return True
                return False

        self._retire_file(True)
        gc.collect()
        return True

    def _fill_metadata(self, response_packet, v3):
        # The announcement: which file this node is serving and how big it is. Written by
        # both the METADATA branch, where the collector asked for it, and the CHUNK branch,
        # where the collector asked for data this node cannot honestly serve.
        #
        # v3 typed METADATA carries chunk_size + total byte length so the receiver's
        # positioned writes stop depending on both ends being configured with the same
        # chunk_size. v2 sent only chunk_count + filename.
        if v3:
            response_packet.set_metadata(self.file.chunk_size, self.file.length,
                                         self.file.get_name())
        else:
            response_packet.set_metadata(self.file.get_length(), self.file.get_name())

    def response(self, packet):
        command = packet.get_command()
        if not Packet.check_command(command):
            return None, None

        v3 = self.protocol_version >= 3
        response_packet = self.new_packet()
        if not v3:
            response_packet.set_source(self.MAC)
            response_packet.set_destination(packet.get_source())
        elif packet.addressing == "sid":
            # Reply under the *session's* sid: mirror the request. In the home direction
            # they coincide; while serving a delegated downlink the session is addressed by
            # the Edge's sid, not this node's own.
            response_packet.set_session(packet.get_session())

        if self.mesh_mode:
            if packet.get_mesh() and packet.get_hop():
                response_packet.enable_mesh()
                if not packet.get_sleep():
                    response_packet.disable_sleep()

        new_sf = None
        # An armed RF-config trial does NOT commit on this short first exchange: the commit
        # criterion is a completed full-payload exchange (a later chunk asked for, or the
        # final-OK), decided in the CHUNK / OK branches below.

        if not v3 and packet.get_debug_hops():
            response_packet.set_data("")
            response_packet.enable_debug_hops()
            response_packet.add_previous_hops(packet.get_message_path())
            response_packet.add_hop(self.name, self.connector.get_rssi(), 0)
            return response_packet, new_sf

        if command == Packet.CHUNK:
            if self.file is None:
                # Nothing to serve: stay silent like a node that isn't serving yet. The
                # poller times out and retries, exactly the pre-transfer behavior.
                return None, new_sf
            if not self.file.metadata_sent:
                # A chunk request for a file this node does not remember announcing.
                #
                # The peer is not wrong that a transfer is open: that is precisely why it
                # is asking for an index mid-file instead of opening with METADATA. What a
                # restart destroyed is THIS end's memory of announcing it, and only the end
                # that cannot remember is in a position to say so. Serving the chunk would
                # answer out of whatever the queue handed back after the restart, and those
                # bytes would be written into a reassembly they do not belong to: every
                # frame honest, the saved file spliced from two.
                #
                # Answering with METADATA rather than going quiet is the point. Silence and
                # a dead link are the same observation, so a peer must never be left to
                # infer a refusal from one; and a collector reads a missing reply as a lost
                # frame and re-asks the same index forever. METADATA answers the question
                # actually at issue: what is this node serving now, and how long is it.
                #
                # This deliberately does NOT record an announcement. Nothing acknowledges a
                # re-announcement, so a lost one leaves the collector repeating this same
                # request, and marking the file announced here would serve that repeat from
                # the new file: the very splice being prevented. Repeating the METADATA
                # instead heals as soon as one of them lands.
                self._fill_metadata(response_packet, v3)
                if self.debug:
                    print("Chunk asked for a file not announced from here; re-announcing {}".format(
                        self.file.get_name()))
                return response_packet, new_sf
            requested_chunk = packet.get_chunk_index() if v3 else int(packet.get_payload().decode())
            # RF trial commit signal: the peer asking for a chunk LATER than the last one we
            # served means the prior full-payload chunk was demodulated on the new config.
            # A re-request of the same (or an earlier) chunk is a stall, not progress.
            prev_sent = self.file.last_chunk_sent
            if self.sf_trial and prev_sent is not None and requested_chunk > prev_sent:
                self._commit_trial()
            response_packet.set_data(self.file.get_chunk(requested_chunk))
            if self.status.subscribers:
                self.status['Chunk'] = self.file.get_length() - requested_chunk
                self.status['Status'] = 'CHUNK'
                self.status['Retransmission'] = self.file.retransmission

            if self.debug:
                print("RC: {} / {}".format(requested_chunk, self.file.get_length()))

            if not self.file.first_sent:
                self.file.report_SST(True)
            return response_packet, new_sf

        if command == Packet.METADATA:    # handle for new file
            # A metadata request heard on the new config proves the peer can still reach us:
            # hold the trial (a reachable-but-idle Edge must not roll back a good config just
            # because no data moved). A stalled CHUNK loop, by contrast, does not hold.
            self._hold_trial()
            if self.file is None:
                return None, new_sf
            filename = self.file.get_name()
            self._fill_metadata(response_packet, v3)

            if self.file.metadata_sent:
                self.file.retransmission += 1
                if self.debug:
                    print("Asked again for Metadata...")
            else:
                self.file.metadata_sent = True

            if self.status.subscribers:
                self.status['File'] = filename
                self.status['Status'] = 'Metadata'
                self.status['Chunk'] = self.file.get_length()
                self.status['Retransmission'] = self.file.retransmission
            return response_packet, new_sf

        if command == Packet.OK:
            response_packet.set_ok()
            # An OK connection poll heard on the new config proves reachability: hold the trial
            # (superseded below if this turns out to be the fire-and-forget final-OK, which
            # commits instead).
            self._hold_trial()

            if (not v3) and packet.get_change_rf():
                new_sf = packet.get_config()
                response_packet.set_change_rf(new_sf)
            elif self.file is not None and self.file.first_sent and not self.file.last_sent:
                if not v3:
                    # Legacy shape, byte-for-byte: any mid-transfer OK finalizes AND is
                    # answered: a v2 requester listens for this reply on its connection
                    # poll, so suppressing it would time that poll out.
                    self.file.sent_ok()
                elif self.file.last_chunk_sent == self.file.get_length() - 1:
                    # Only an OK arriving after the tail chunk was served can be the
                    # initiator's fire-and-forget final-OK: it ends the transfer and
                    # nobody listens for a reply, so answering would only burn airtime.
                    # An OK any earlier is a connection poll (e.g. the authority
                    # re-polling after a reboot) and gets its keepalive answer below:
                    # a half-sent file must never be marked sent by a poll.
                    self.file.sent_ok()
                    # The final-OK is the whole file landing on the new config: the
                    # full-exchange proof for a single-chunk file, which never triggers the
                    # next-chunk commit signal.
                    self._commit_trial()
                    return None, new_sf
            return response_packet, new_sf

        return response_packet, new_sf

    # =====================================================================================
    # Drive side. The initiator loop: poll a peer, pull METADATA + CHUNKs, reassemble.
    # =====================================================================================

    def create_request(self, destination, mesh_active, sleep_mesh, session_id=None):
        if self.protocol_version >= 3:
            packet = self.new_packet()          # v3 frame, session-id addressed
            if session_id is not None:
                packet.set_session(session_id)
        else:
            packet = Packet(self.mesh_mode, self.short_mac)
            packet.set_source(self.connector.get_mac())
            packet.set_destination(destination)
        if mesh_active:
            packet.enable_mesh()
            if not sleep_mesh:
                packet.disable_sleep()
        return packet

    def send_request(self, packet: Packet) -> Packet:
        if self.mesh_mode and self.protocol_version < 3:   # v3 mesh uses seq, not a random id
            packet.set_id(self.generate_id())
            if self.debug_hops:
                packet.enable_debug_hops()

        self.time_since_last_request = ticks_diff(time(), self.time_request)
        self.time_request = time()

        # One initiator round via the shared verb (wraps the connector's send_and_wait_response).
        response_packet, packet_size_sent, packet_size_received, time_pr = self.request(packet)

        # Remember what kind landed in the reply slot (None on timeout/error): a delegated
        # drive uses it to hear the authority's contending poll and yield.
        if response_packet is None or isinstance(response_packet, dict):
            self._last_reply_kind = None
        else:
            self._last_reply_kind = response_packet.get_command()

        if self.status.subscribers:
            self.status['PSizeS'] = packet_size_sent
            self.status['PSizeR'] = packet_size_received
            self.status['TimePR'] = time_pr * 1000  # Time in ms
            self.status['TimeBtw'] = self.time_since_last_request * 1000  # Time in ms
            self.status['RSSI'] = self.connector.get_rssi()
            self.status['SNR'] = self.connector.get_snr()

            if isinstance(response_packet, dict):  # Handle errors
                self.status['Retransmission'] += 1
                if response_packet.get("type") == "CORRUPTED_PACKET":
                    self.status['CorruptedPackets'] += 1
                if self.debug:
                    print("Error received during request: ", response_packet)
                return None  # Signal failure

        elif isinstance(response_packet, dict):
            if self.debug:
                print("Error received during request: ", response_packet)
            return None

        return response_packet  # Return valid packet if successful

    def perform_handshake(self, digital_endpoint, tries=5):
        """As the drive side (the handshake responder + sid-assigner), drive the two-round ECDH
        with a serve-side peer over open MAC CTRL frames and store the resulting Session. Each
        round is retried up to `tries` times so a dropped handshake frame recovers (a retried
        INIT gets a fresh ephemeral; a retried WELCOME gets an idempotent re-ACK). Returns the
        Session, or None if a round never lands. The peer authenticates nothing here: in
        `secure` this is confidentiality-only; the gateway accepts the peer by its registered
        identity, and the catastrophic downlink is guarded separately."""
        from AlLoRa.Security.handshake import responder_accept
        from AlLoRa.Security.ec_p256 import generate_private_key
        if self.static_priv is None:
            self.static_priv = generate_private_key(urandom)

        # Address follows registration: a device_id-registered peer takes the v3 did-addressed
        # path (no MAC on the wire); a MAC-registered one keeps the legacy two-MAC handshake.
        did = digital_endpoint.get_did()
        if did is not None:
            addressing, token = "did", did
            # The sid rides the WELCOME only when we had to move it off the derived value to
            # break a clash. Otherwise both ends already derive device_id[0].
            send_sid = digital_endpoint.session_id != digital_endpoint.derived_sid()
        else:
            addressing, token = "mac", digital_endpoint.get_mac_address()
            send_sid = True    # no shared identity to derive the sid from; it must be sent
        sid = digital_endpoint.session_id

        # round 1: prompt the peer for its ephemeral public key
        hello = None
        for _ in range(tries):
            hello = self.send_request(self._ctrl_packet(token, Node._HS_INIT, addressing=addressing))
            if self._is_hs(hello, Node._HS_HELLO):
                break
        if not self._is_hs(hello, Node._HS_HELLO):
            return None
        session, welcome = responder_accept(self.static_priv, hello.get_payload()[1:], sid,
                                            send_sid=send_sid)

        # round 2: send our static public key (+ sid only on reassignment), expect the ack
        for _ in range(tries):
            if self._is_hs(self.send_request(
                    self._ctrl_packet(token, Node._HS_WELCOME, welcome, addressing=addressing)),
                    Node._HS_ACK):
                self.session_store.put(session)
                return session
        return None

    @staticmethod
    def _is_hs(packet, hs_kind):
        # A valid handshake reply: a CTRL frame whose 1-byte type prefix matches.
        return (packet is not None and packet.get_command() == Packet_v3.CTRL
                and packet.get_payload() and packet.get_payload()[0] == hs_kind)

    def ask_ok(self, packet: Packet):
        packet.set_ok()
        response_packet = self.send_request(packet)
        # A round that heard nothing back is an ordinary outcome on a lossy link, not an
        # error. Report it as the same "no usable answer" the other arms return, so the
        # caller retries on its normal cadence instead of raising from every lost frame.
        if response_packet is None:
            return None, None
        if self.save_hops(response_packet):
            return  (1, "hop_catch.json"), response_packet.get_hop()
        if response_packet.get_command() == Packet.OK:
            hop = response_packet.get_hop()
            return True, hop
        return None, None

    def ask_metadata(self, packet: Packet):
        packet.ask_metadata()
        response_packet = self.send_request(packet)
        if response_packet is None:      # nothing came back
            return None, None
        if self.save_hops(response_packet):
            return  (1, "hop_catch.json"), response_packet.get_hop()
        if response_packet.get_command() == Packet.METADATA:
            try:
                metadata = response_packet.get_metadata()
                self._wire_chunk_size = metadata.get("CHUNK_SIZE")  # v3 carries it; None for v2
                # Kept, not dropped: the byte total is what tells a full-size chunk
                # standing in for the real short tail apart from the real one. Without
                # it a file is only ever counted complete, never measured.
                self._wire_total_len = metadata.get("TOTAL_LEN")    # v3 carries it; None for v2
                hop = response_packet.get_hop()
                length = metadata["LENGTH"]
                filename = metadata["FILENAME"]
                if self.status.subscribers:
                    self.status['File'] = filename
                return (length, filename), hop
            except:
                return None, None
        return None, None

    def ask_data(self, packet: Packet, next_chunk):
        packet.ask_data(next_chunk)
        response_packet = self.send_request(packet)
        if response_packet is None:      # nothing came back
            return None, None
        if self.save_hops(response_packet):
            return b"0", response_packet.get_hop()
        if response_packet.get_command() == Packet.METADATA:
            # The peer answered a request for a chunk by announcing what it is serving.
            # It does not remember announcing the file this reassembly is for, so the
            # bytes to continue with are not coming and asking again cannot produce them.
            # A third answer, distinct from the None that means nothing came back: that
            # one is a lost frame and is answered by re-asking the same index.
            return Node.RE_ANNOUNCED, response_packet.get_hop()
        if response_packet.get_command() == Packet.DATA:
            try:
                chunk = response_packet.get_payload()
                if self.mesh_mode:
                    id = response_packet.get_id()
                    if not self.check_id_list(id):
                        return None, None
                    hop = response_packet.get_hop()
                    if self.debug and hop:
                        print("CHUNK + HOP: {} -> {} - Node: {}".format(chunk, hop, self.source_mac))
                    return chunk, hop
                else:
                    if self.debug:
                        print("CHUNK: {} - Node: {}".format(chunk, self.source_mac))
                    return chunk, None

            except Exception as e:
                if self.debug:
                    print("ASKING DATA ERROR: {} Node {}".format(e, self.source_mac))
                return None, None
        return None, None

    @staticmethod
    def _reopen_transfer(digital_endpoint):
        """Drop what has been collected and go back to asking METADATA.

        The repair for every answer that is about a different file than the one being
        assembled, whether it disagreed by its length or the peer said so outright. Nothing
        already collected can be trusted to belong together, and METADATA is the only
        exchange that says what the peer is serving now and how long it is. The reassembly
        goes through set_current_file, which releases its writer and temp file.
        """
        digital_endpoint.set_current_file(None)
        digital_endpoint.state = Digital_Endpoint.REQUEST_DATA_STATE

    def listen_to_endpoint(self, digital_endpoint: Digital_Endpoint, listening_time=None,
                       print_file=False, save_file=False, one_file=False,
                       stall_timeout=None):
        stop = False

        self._prepare_drive()

        # Two different things, kept apart on purpose. `mac` is a wire address and is used for
        # nothing else: only a v2 frame carries it. `label` is how this peer is named off the
        # air (folder, topic, status line, logs), and for a device_id-registered endpoint there
        # is no MAC to use, so it comes from the identity instead. Naming the folder by the MAC
        # gave every registered peer the same "00000000".
        mac = digital_endpoint.get_mac_address()
        label = digital_endpoint.get_label()
        self.source_mac = label

        if self.status.subscribers:
            self.status['SMAC'] = label
        save_to = self.result_path + "/" + label
        sleep_mesh = digital_endpoint.get_sleep()

        connector_ok = self.prepare_connector(digital_endpoint)

        if not connector_ok:
            if self.debug:
                print("Connector not ready for endpoint: ", label)
            return False

        # Secure first contact: establish a session (ECDH handshake) before the transfer, once
        # per session lifetime. The RAM store keeps it across subsequent listens. If it fails
        # there is nothing to protect the transfer with, so give up this endpoint for now.
        if self.security_mode == 'secure' and self.session_store is not None \
                and self.session_store.get(digital_endpoint.session_id) is None:
            if self.home_role == "source":
                # Only the authority drives the handshake: it holds the static key and assigns
                # the sid. A source-home node is driving only because it was delegated, and it
                # knows its peer solely through the live session, so with no session there is
                # neither a party to be here nor an address to reach. Come home and let the
                # authority reclaim and re-handshake on its own poll.
                if self.debug:
                    print("No session for the delegated pull; coming home")
                return False
            if self.perform_handshake(digital_endpoint) is None:
                if self.debug:
                    print("Handshake failed with endpoint: ", label)
                return False

        t0 = time()
        if listening_time is None or listening_time == float('inf'):
            end_time = None    # listen forever (a wrapped deadline can't express it)
        else:
            end_time = ticks_add(t0, listening_time * 1000)

        # Two deadlines, measuring different things, because one cannot do both jobs.
        #
        # `listening_time` is a ceiling on the whole drive. It is what a caller polling for 60 s
        # means, and it must keep meaning that: a peer that answers every poll would otherwise
        # hold an idle poll open forever.
        #
        # `stall_timeout` is the one that ends a drive that has stopped getting anywhere. It
        # resets on PROGRESS, not on traffic: a chunk index later than the last one asked for.
        # Answering is not progress. The same chunk requested over and over must still end the
        # drive, which is the rule the RF trial already applies to a held configuration.
        #
        # Adding it can only make this method return earlier than before, never later, so no
        # existing caller can hang longer than it does today.
        #
        # Before this existed, `listening_time` was doing both jobs and could do neither well.
        # A 1 MiB downlink needs about 3000 s, so the ceiling had to be set above the transfer
        # time by whoever configured it. Set too low it cut a healthy pull mid-flight, with
        # every chunk still arriving at full signal, and the pull restarted from chunk 0.
        # Measured on 2026-08-18: open stopped at chunk 3518 of 4212 on a 2400 s ceiling and
        # secure at 4180 of 4246 on a 3000 s one, each to the second.
        stall_deadline = None
        if stall_timeout is not None and stall_timeout != float('inf'):
            stall_deadline = ticks_add(time(), int(stall_timeout * 1000))
        last_progress = -1

        # RF-config probe bookkeeping for this visit: did we hear the peer at all (locate), and
        # did a full uplink file complete (commit)? Consumed by _probe_visit_end at visit end.
        probe_heard = False
        probe_completed = False
        self._peer_alive_this_visit = False

        while (end_time is None or ticks_diff(end_time, time()) > 0) and \
                (stall_deadline is None or ticks_diff(stall_deadline, time()) > 0):
            t0 = time()
            delegated = False

            try:
                packet_request = self.create_request(mac, digital_endpoint.get_mesh(), sleep_mesh,
                                                     digital_endpoint.session_id)

                # The safe boundary for a downlink delegation: any idle point between
                # complete files (pre-contact OK, or the idle metadata-poll loop), but
                # never mid-chunk (a reassembly in progress must finish first). With a
                # downlink pending, the Hub preset delegates the collector role here (GRANT +
                # serve + reclaim) instead of running this round's request.
                if digital_endpoint.state != "PROCESS_CHUNK_STATE" \
                        and self._maybe_delegate(digital_endpoint):
                    delegated = True
                    t0 = time()

                elif digital_endpoint.state == "REQUEST_DATA_STATE":
                    if self.debug:
                        print("ASKING METADATA to {}".format(label))
                    metadata, hop = self.ask_metadata(packet_request)
                    t0 = time()
                    if self._heard_authority_poll():
                        return False
                    # v3 typed METADATA carries the sender's chunk_size; v2 falls back to
                    # our own configured chunk_size (the matched-config stopgap).
                    cks = self._wire_chunk_size or self.chunk_size
                    digital_endpoint.set_metadata(metadata, hop, self.mesh_mode, save_to, cks,
                                                  total_len=self._wire_total_len)
                    if self.debug:
                        print("METADATA from {}: {}".format(label, metadata))

                elif digital_endpoint.state == "PROCESS_CHUNK_STATE":
                    next_chunk = digital_endpoint.get_next_chunk()
                    if next_chunk is not None:
                        # Progress, and the only thing that resets the stall deadline. The index
                        # the collector wants next only advances once the previous chunk is in
                        # the reassembly, so this is the collector's own evidence that the
                        # transfer is moving, not merely that the peer is talking.
                        if stall_deadline is not None and next_chunk > last_progress:
                            last_progress = next_chunk
                            stall_deadline = ticks_add(time(), int(stall_timeout * 1000))
                        if self.debug:
                            print("ASKING CHUNK: {} to {}".format(next_chunk, label))
                        data, hop = self.ask_data(packet_request, next_chunk)
                        t0 = time()
                        if self._heard_authority_poll():
                            return False
                        self.status['Chunk'] = digital_endpoint.file_reception_info["total_chunks"] - next_chunk
                        if data is Node.RE_ANNOUNCED:
                            # The peer says it is serving something else: it came back from
                            # a restart with no memory of announcing this file, so it
                            # announced what it holds now instead of answering with data.
                            # Nothing it can send would continue this reassembly.
                            self._reopen_transfer(digital_endpoint)
                            if self.debug:
                                print("{} re-announced mid-transfer: re-asking METADATA".format(label))
                            file = None
                        else:
                            file = digital_endpoint.set_data(data, hop, self.mesh_mode)
                        if file is Digital_Endpoint.CHUNK_REFUSED:
                            # The peer answered with bytes from some other file, so every
                            # chunk already collected may belong to a file it no longer
                            # holds. Re-asking this index would only ask the same question
                            # again: the answer was not lost or damaged, it was about
                            # something else.
                            self._reopen_transfer(digital_endpoint)
                            if self.debug:
                                print("Chunk refused by {}: re-asking METADATA".format(label))
                        elif file:
                            # The sink is fed BEFORE the fire-and-forget final-OK: that OK
                            # retires the file on the serving side (and pops a delegated
                            # downlink off its queue), so a sink failure after it would
                            # lose the file with no retry left anywhere. Failing here
                            # leaves the transfer unacknowledged and the next round pulls
                            # the file again. Delivery is at-least-once, and a lost
                            # final-OK may feed an (idempotent) sink twice.
                            if print_file:
                                print(file.get_content())
                            if save_file:
                                try:
                                    self.data_sink.consume(
                                        file, self._reception_context(digital_endpoint, label))
                                except Exception:
                                    # No ack for an unconsumed file, and no OK poll
                                    # either: to a peer whose last chunk went out, an OK
                                    # is exactly the final-OK and would retire the file.
                                    # Rewind to re-pull it whole next round instead.
                                    file.discard()
                                    digital_endpoint.state = Digital_Endpoint.REQUEST_DATA_STATE
                                    raise
                            final_ok = self.create_request(mac, digital_endpoint.get_mesh(),
                                                           sleep_mesh, digital_endpoint.session_id)
                            final_ok.set_ok()
                            if self.protocol_version < 3:
                                final_ok.set_source(self.connector.get_mac())
                            sleep(1)
                            self.send_lora(final_ok)
                            self.status['Chunk'] = "DONE"
                            # A full uplink file completed on the active config: the RF-config
                            # probe's commit signal (the peer is here AND the link carries a
                            # max-payload chunk, not just a poll).
                            probe_completed = True
                            if one_file:
                                stop = True

                elif digital_endpoint.state == "OK":
                    if self.debug:
                        print("ASKING OK to {}".format(label))
                    ok, hop = self.ask_ok(packet_request)
                    t0 = time()
                    digital_endpoint.connected(ok, hop, self.mesh_mode)

                # Did this round hear anything back? The one signal both the RF-config probe
                # and the sleep controller read: a reply locates the peer on the active config
                # (silence advances the probe to the other one) and says the inter-request gap
                # is one the link tolerates.
                heard = self._last_reply_kind is not None
                if not delegated and heard:
                    probe_heard = True
                    self._peer_alive_this_visit = True

                if self.sf_trial and self.protocol_version < 3:
                    # Legacy v2 drive-side trial (a Collector changing its own config via
                    # ask_change_rf): commit on the first successful round, as v2 always did.
                    # In v3 the trial is owned by the source role and resolved by a full-payload
                    # exchange (see `response`); the drive loop only carries the window backstop.
                    self._commit_trial()

                if not delegated:
                    # A delegation round served nobody a request: it says nothing about
                    # the inter-request gap this link tolerates, so it must not feed
                    # the sleep controller's hunt. A round that did go out and heard
                    # nothing back is exactly the failed round that hunt backs off from.
                    if heard:
                        self.pacing.on_success()
                    else:
                        self.pacing.on_failure()

            except Exception as e:
                if self.debug:
                    print("LISTEN_TO_ENDPOINT ERROR: {} Node {}".format(e, label))

                dt = ticks_diff(time(), t0) / 1000

                self.pacing.on_failure()

            finally:
                # The self-restore backstop runs every drive turn (success or failure): an armed
                # trial that never confirms falls back to last-known-good once its window elapses.
                self._service_trial_window()

                if self.status.subscribers:
                    self.status['Status'] = digital_endpoint.state
                    self.status.notify()

                gc.collect()
                dt = ticks_diff(time(), t0) / 1000
                if self.debug:
                    print("DT: ", dt, "Sleep time: ", self.NEXT_ACTION_TIME_SLEEP)
                sleep_time = self.pacing.next_sleep()
                if self.debug:
                    print("Sleep time: ", sleep_time)
                if sleep_time > 0:
                    sleep(sleep_time)

            # Outside the finally on purpose: a `break` inside it silently discards
            # any exception still propagating from the try/except (and is a hard
            # SyntaxError on newer Pythons). The one-file drive sets `stop` in the
            # try with no exception in flight, so breaking here, after the finally's
            # notify/gc/sleep cleanup, is behavior-identical and safe.
            if stop:
                break

        # Visit over: advance any RF-config probe once (round-robin fairness — one probe per
        # visit, never starve other endpoints to chase one reconfig). No-op unless this is a
        # Hub with a live {new, old} trial for this endpoint.
        self._probe_visit_end(digital_endpoint, probe_heard, probe_completed)

        # Also visit-scoped: is the secure session we hold for this endpoint still usable? A
        # peer that rebooted lost its half and cannot tell us, so the authority has to notice.
        self._session_visit_end(digital_endpoint)

        # True only when a one_file drive completed (its file reached the sink):
        # a granted pull uses this to tell a delivered delegation from a dead one.
        return stop

    def _reception_context(self, digital_endpoint, label):
        # Freeze a completion record for the sink: identity + a final RF/quality snapshot, taken
        # now (the endpoint is reused for the next file). Every field is best-effort: a missing
        # RSSI/did must never break delivery, so each lookup is guarded.
        from AlLoRa.DataSinks.DataSink import Reception
        did = None
        try:
            did = digital_endpoint.get_did()
        except Exception:
            pass
        rssi = snr = None
        try:
            rssi = self.connector.get_rssi()
        except Exception:
            pass
        try:
            snr = self.connector.get_snr()
        except Exception:
            pass
        total_chunks = None
        info = getattr(digital_endpoint, "file_reception_info", None)
        if isinstance(info, dict):
            total_chunks = info.get("total_chunks")
        return Reception(source=label, session_id=digital_endpoint.session_id,
                         device_id=did, rssi=rssi, snr=snr,
                         total_chunks=total_chunks, timestamp_ms=time())

    def save_hops(self, packet):
        if packet is None:
            return False
        if packet.get_debug_hops():
            hops = packet.get_message_path()
            id = packet.get_id()
            t = get_time()  #strftime("%Y-%m-%d_%H:%M:%S")
            line = "{}: ID={} -> {}\n".format(t, id, hops)
            with open('log_rssi.txt', 'a') as log:
                log.write(line)
            return True
        return False

    def ask_change_rf(self, digital_endpoint, new_config):
        # v2 only. The request below carries the change in the flag byte's bit 7, and a v3 node
        # ignores that flag on receipt, so on a v3 link this would transmit twenty frames
        # nobody acts on and then report a failed exchange: a missing transport dressed as a
        # bad antenna. A v3 caller wants the Hub's override, which selects a transport.
        if self.protocol_version >= 3:
            if self.debug:
                print("The legacy in-band RF change has no transport on a v3 link")
            return False
        try_for = 20
        new_config = [new_config.get("freq", None), new_config.get("sf", None),
                        new_config.get("bw", None), new_config.get("cr", None),
                        new_config.get("tx_power", None),
                        new_config.get("cks", None)]
        config = self.connector.get_rf_config()
        if self.debug:
            print("Current config: ", config)
            print("New config: ", new_config)
        # Only change the values that are different from the current configuration
        new_freq = new_config[0] if new_config[0] != config[0] else None
        new_sf = new_config[1] if new_config[1] != config[1] else None
        new_bw = new_config[2] if new_config[2] != config[2] else None
        new_cr = new_config[3] if new_config[3] != config[3] else None
        new_tx_power = new_config[4] if new_config[4] != config[4] else None
        new_chunk_size = new_config[5] if new_config[5] != self.chunk_size else None
        while True:
            packet = Packet(self.mesh_mode, self.short_mac)
            packet.set_destination(digital_endpoint.get_mac_address())
            changes = packet.set_change_rf({"freq": new_freq, "sf": new_sf,
                                            "bw": new_bw, "cr": new_cr,
                                            "tx_power": new_tx_power,
                                            "cks": new_chunk_size})
            if not changes:
                return False
            if digital_endpoint.get_mesh():
                packet.enable_mesh()
                if not digital_endpoint.get_sleep():
                    packet.disable_sleep()
            response_packet = self.send_request(packet)
            try:
                if response_packet is not None and response_packet.get_command() == Packet.OK:
                    new_config = response_packet.get_config()
                    if self.debug:
                        print("OK and changing config to: ", new_config)
                    changed = self.change_rf_config(new_config)
                    if not changed:
                        return False

                    self.status.notify()
                    self.reset_sleep_time()
                    return True
                else:
                    try_for -= 1
                    if try_for <= 0:
                        return False
            except Exception as e:
                if self.debug:
                    print("Error changing RF config: ", e)
                try_for -= 1
                if try_for <= 0:
                    return False

    def reset_sleep_time(self):
        # Recompute the sf/bw-derived bounds (they may have changed with the RF config) and
        # hand them to Pacing, which resets the controller to a fresh hunt. Unlike init, the
        # max here is the ToA-derived one, matching the original reset_sleep_time.
        self.pacing.set_sleep_bounds(*self.calculate_sleep_time_bounds())
        if self.debug:
            print("Reset sleep time to:", self.NEXT_ACTION_TIME_SLEEP)

    def calculate_sleep_time_bounds(self):
        sf = self.connector.sf
        bw = self.connector.bw
        # Basic heuristic to calculate min and max sleep times based on SF and BW
        sf_factor = 2 ** (sf - 7)  # SF7 as baseline
        bw_factor = 250 / bw  # 500kHz as baseline
        base_min_sleep_time = 0.001  # Adjust as needed
        base_max_sleep_time = 0.5  # Adjust as needed
        min_sleep_time = base_min_sleep_time * bw_factor / sf_factor
        max_sleep_time = base_max_sleep_time * sf_factor / bw_factor
        if self.debug:
            print("Min sleep time: ", min_sleep_time, "Max sleep time: ", max_sleep_time)
        return min_sleep_time, max_sleep_time

    def resolve_endpoint_rf(self, digital_endpoint):
        """Give an endpoint concrete RF, filling whatever it left unstated from this node's
        own config. Idempotent, so it is safe on every path that can produce an endpoint:
        registration from a file, registration by hand, and one handed straight to
        listen_to_endpoint without being registered at all."""
        if not digital_endpoint.resolve_rf(self.rf_defaults):
            return
        if self.debug:
            # Naming the source is the point: an endpoint on the wrong config and an endpoint
            # correctly following this node look identical once resolved.
            print("Endpoint {} ({}): RF {} (from {})".format(
                digital_endpoint.get_name(), digital_endpoint.get_label(),
                digital_endpoint.describe_rf(), digital_endpoint.rf_source))
            if digital_endpoint.rf_skipped:
                print("  ignored non-RF keys: {}".format(", ".join(digital_endpoint.rf_skipped)))

    def prepare_connector(self, digital_endpoint):
        if self.debug:
            print("Preparing connector for endpoint: ", digital_endpoint)
        self.resolve_endpoint_rf(digital_endpoint)
        de_freq = digital_endpoint.freq
        de_sf = digital_endpoint.sf
        de_bw = digital_endpoint.bw
        de_cr = digital_endpoint.cr
        de_tx_power = digital_endpoint.tx_power

        freq, sf, bw, cr, tx_power = self.connector.get_rf_config()
        if self.debug:
            print("Current RF config: ", freq, sf, bw, cr, tx_power)
            print("Endpoint RF config: ", de_freq, de_sf, de_bw, de_cr, de_tx_power)
        if de_freq != freq or de_sf != sf or de_bw != bw or de_cr != cr or de_tx_power != tx_power:
            if self.debug:
                print("Changing RF config to: ", de_freq, de_sf, de_bw, de_cr, de_tx_power)
            # try 3 times to change the RF config to fit the endpoint configuration
            for i in range(3):
                success = self.connector.change_rf_config(frequency=de_freq, sf=de_sf, bw=de_bw, cr=de_cr, tx_power=de_tx_power)
                if success:
                    sleep(1)
                    for i in range(3):
                        rf_params = self.connector.get_rf_config()
                        if rf_params:
                            # Check that the RF configuration has been changed successfully
                            if rf_params[0] == de_freq and rf_params[1] == de_sf and rf_params[2] == de_bw and rf_params[3] == de_cr and rf_params[4] == de_tx_power:
                                if self.debug:
                                    print("RF configuration changed successfully")
                                return True
                            break
                        sleep(1)
                sleep(1)
            if self.debug:
                print("Failed to change RF configuration")
            return False    # Failed to change RF configuration
        if self.debug:
            print("RF config already set to endpoint config")
        return True
