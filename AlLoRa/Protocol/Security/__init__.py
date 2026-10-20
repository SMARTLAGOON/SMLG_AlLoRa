"""v3 secure-mode building blocks: the session model, anti-replay, and (later) the
crypto-backend + handshake seams. Crypto-agnostic where it can be: the session model
treats key material as opaque bytes so the AEAD backend is swappable."""
