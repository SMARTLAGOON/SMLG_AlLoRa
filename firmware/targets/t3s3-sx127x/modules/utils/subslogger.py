# Based on: https://github.com/majoson-chen/micropython-ulogger/tree/main?tab=readme-ov-file#readme
# Thanks to the authors!

import os
import ujson
from utime import time, sleep, sleep_ms

DEBUG = 10
INFO = 20
WARN = 30
ERROR = 40
CRITICAL = 50

TO_FILE = 100
TO_TERM = 200

_level = 0
_msg = 1
_time = 2
_name = 3
_fnname = 4

def level_name(level, color=False):
    if not color:
        return ["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"][level//10-1]
    else:
        return [
            "\033[37mDEBUG\033[0m", "\033[97mINFO\033[0m", "\033[93mWARN\033[0m", "\033[35mERROR\033[0m", "\033[91mCRITICAL\033[0m"
        ][level//10-1]

class BaseClock():
    def __call__(self):
        return '%d' % time()

class Handler():
    def __init__(self, level=INFO, colorful=True, fmt="%(time)s - %(level)s - %(name)s - %(msg)s", clock=None,
                 direction=TO_TERM, file_name="logs/log.log", max_file_size=4096, buffer_size=10):
        self._direction = direction
        self.level = level
        self._clock = clock if clock else BaseClock()
        self._color = colorful
        self._file_name = file_name if direction == TO_FILE else ''
        self._max_size = max_file_size if direction == TO_FILE else 0
        self.buffer_size = buffer_size
        self.buffer = []

        if direction == TO_FILE:
            self._file = open(file_name, 'a+')

        self._map = bytearray()
        idx = 0
        while True:
            idx = fmt.find("%(", idx)
            if idx >= 0:
                a_idx = fmt.find(")s", idx + 2)
                if a_idx < 0:
                    raise Exception("Unable to parse text format successfully.")
                text = fmt[idx + 2:a_idx]
                idx = a_idx + 2
                if text == "level":
                    self._map.append(_level)
                elif text == "msg":
                    self._map.append(_msg)
                elif text == "time":
                    self._map.append(_time)
                elif text == "name":
                    self._map.append(_name)
                elif text == "fnname":
                    self._map.append(_fnname)
            else:
                break

        self._template = fmt.replace("%(level)s", "%s") \
            .replace("%(msg)s", "%s") \
            .replace("%(time)s", "%s") \
            .replace("%(name)s", "%s") \
            .replace("%(fnname)s", "%s") \
            + "\n" if fmt[-1] != '\n' else ''

    def _msg(self, *args, level, name, fnname):
        if level < self.level:
            return

        temp_map = []
        text = ''
        for item in self._map:
            if item == _msg:
                for text_ in args:
                    text = "%s%s" % (text, text_)
                temp_map.append(text)
            elif item == _level:
                temp_map.append(level_name(level, self._color) if self._direction == TO_TERM else level_name(level))
            elif item == _time:
                temp_map.append(self._clock())
            elif item == _name:
                temp_map.append(name)
            elif item == _fnname:
                temp_map.append(fnname if fnname else "unknownfn")

        if self._direction == TO_TERM:
            self._to_term(tuple(temp_map))
        else:
            self.buffer.append(self._template % tuple(temp_map))
            if len(self.buffer) >= self.buffer_size:
                self.flush_buffer()

    def _to_term(self, map):
        print(self._template % map, end='')

    def _to_file(self, data):
        with open(self._file_name, 'a') as fp:
            fp.write(data)
            fp.flush()

    def flush_buffer(self):
        if self.buffer:
            self._to_file(''.join(self.buffer))
            self.buffer = []

    def _rotate_logs(self):
        base, ext = os.path.splitext(self._file_name)
        for i in range(4, 0, -1):
            src = f"{base}.{i}{ext}"
            dst = f"{base}.{i+1}{ext}"
            if os.path.exists(src):
                os.rename(src, dst)
        os.rename(self._file_name, f"{base}.1{ext}")

class ULogger():
    def __init__(self, name, handlers=None):
        self.name = name
        self._handlers = handlers if handlers else [Handler()]

    @property
    def handlers(self):
        return self._handlers

    def _msg(self, *args, level, fn):
        for item in self._handlers:
            item._msg(*args, level=level, fnname=fn, name=self.name)

    def debug(self, *args, fn=None):
        self._msg(*args, level=DEBUG, fn=fn)

    def info(self, *args, fn=None):
        self._msg(*args, level=INFO, fn=fn)

    def warn(self, *args, fn=None):
        self._msg(*args, level=WARN, fn=fn)

    def error(self, *args, fn=None):
        self._msg(*args, level=ERROR, fn=fn)

    def critical(self, *args, fn=None):
        self._msg(*args, level=CRITICAL, fn=fn)

class Logger:
    def __init__(self, log_file="/sd/logs/log.log", always_log_topics=None, change_log_topics=None):
        self.log_file = log_file
        self.always_log_topics = always_log_topics or []
        self.change_log_topics = change_log_topics or []
        self.previous_status = {}

        if not self.path_exists("/sd/logs"):
            os.mkdir("/sd/logs")

        self.logger = ULogger(
            name="Logger",
            handlers=[Handler(
                level=INFO,
                fmt="%(time)s - %(level)s - %(name)s - %(msg)s",
                clock=None,
                direction=TO_FILE,
                file_name=log_file,
                max_file_size=4096,
                buffer_size=10
            )]
        )

    def path_exists(self, path):
        try:
            os.listdir(path)
            return True
        except OSError:
            return False

    def update(self, status):
        log_entry = {"status": {}}

        for topic in self.always_log_topics:
            if topic in status:
                log_entry["status"][topic] = status[topic]

        for topic in self.change_log_topics:
            if topic in status and self.previous_status.get(topic) != status[topic]:
                log_entry["status"][topic] = status[topic]

        if log_entry["status"]:
            self.write_log(log_entry)

        self.previous_status = status.copy()

    def write_log(self, log_entry):
        self.logger.info(ujson.dumps(log_entry))

# Example usage
if __name__ == "__main__":
    logger = Logger(log_file="/sd/logs/log.log", always_log_topics=["RSSI", "SNR"], change_log_topics=["File"])
    status = {"RSSI": -70, "SNR": 10, "File": "test.txt"}
    for _ in range(10):
        logger.update(status)
        sleep(1)
