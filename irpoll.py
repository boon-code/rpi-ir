import sys
import os
import re
import tty
import time
import json
import fcntl
import socket
import select
import errno
import struct
import requests
import argparse
import traceback
import logging
import threading
import traceback
import ctypes
import termios
import optparse
import subprocess

try:
    import queue
except ImportError:
    import Queue as queue

termios.c_iflag = 0
termios.c_oflag = 1
termios.c_cflag = 2
termios.c_lflag = 3
termios.ispeed = 4
termios.ospeed = 5
termios.c_cc = 6

_DEFAULT_LOG_FORMAT = "%(name)s : %(threadName)s : %(levelname)s \
: %(message)s"

libc = ctypes.cdll.LoadLibrary("libc.so.6")


class PollException(Exception):
    pass


class AlreadyAssignedError(PollException):
    pass


class BadFDError(PollException):
    pass


class timespec_t(ctypes.Structure):
    libc = libc
    _fields_ = [
        ("tv_sec", libc.time.restype),
        ("tv_nsec", ctypes.c_long),
    ]

    def __init__(self, nanosec=0):
        ctypes.Structure.__init__(self)
        self.set_value(nanosec)

    def set_value(self, seconds, nanosec=None):
        self.tv_sec = seconds
        if nanosec is not None:
            ov_sec = int(nanosec/(10**9))
            self.tv_nsec = int(nanosec - (ov_sec * (10**9)))
            self.tv_sec += ov_sec
        else:
            self.tv_nsec = 0


class itimerspec_t(ctypes.Structure):
    _fields_ = [
        ("it_interval", timespec_t),
        ("it_value", timespec_t)
    ]

    def __init__(self, nano_interval=0, nano_value=0):
        ctypes.Structure.__init__(self)
        self.set_value(0)

    def set_value(self, seconds=0, nanosec=None, repeated=False):
        if repeated:
            self.it_value.set_value(seconds, nanosec)
            self.it_interval.set_value(seconds, nanosec)
        else:
            self.it_value.set_value(seconds, nanosec)
            self.it_interval.set_value(0)


class TimerFD(object):
    TIMER_ABSTIME = 0x00000001
    CLOCK_REALTIME = 0
    CLOCK_MONOTONIC = 1
    libc = libc

    def __init__(self, clockid=CLOCK_MONOTONIC):
        self._fd = self.libc.timerfd_create(clockid, 0)
        self._old = itimerspec_t()
        self._new = itimerspec_t()

    def set_rtime(self, seconds, nanosec=None, repeated=False):
        assert self._fd >= 0, "invalid fd"
        self._new.set_value(seconds, nanosec, repeated)
        libc.timerfd_settime(self._fd, 0, ctypes.pointer(self._new),
                             ctypes.pointer(self._old))

    def is_armed(self):
        if (self._new.it_value.tv_sec == 0) and (self._new.it_value.tv_nsec == 0):
            return False
        else:
            return True

    def disarm(self):
        assert self._fd >= 0, "invalid fd"
        self.set_rtime(0)

    def wait(self):
        assert self._fd >= 0, "invalid fd"
        data = os.read(self._fd, 8)
        return struct.unpack("Q", data)[0]

    def close(self):
        assert self._fd >= 0, "invalid fd"
        os.close(self._fd)
        self._fd = -1

    def fileno(self):
        assert self._fd >= 0, "invalid fd"
        return self._fd


class EventFD(object):
    libc = libc
    def __init__(self, init_value=0, flags=0):
        self._fd = self.libc.eventfd(init_value, flags)

    def wait(self):
        assert self._fd >= 0, "invalid fd"
        return os.read(self._fd, 8)

    def notify(self, value=1):
        assert self._fd >= 0, "invalid fd"
        os.write(self._fd, ctypes.c_ulonglong(value))

    def close(self):
        assert self._fd >= 0, "invalid fd"
        os.close(self._fd)
        self._fd = -1

    def fileno(self):
        assert self._fd >= 0, "invalid fd"
        return self._fd


def set_block_mode(fd, blocking):
    flags = fcntl.fcntl(fd, fcntl.F_GETFL, 0)
    old_flags = flags
    if blocking:
        flags &= ~os.O_NONBLOCK
    else:
        flags |= os.O_NONBLOCK
    fcntl.fcntl(fd, fcntl.F_SETFL, flags)
    return old_flags


class PollEntry(object):
    def __init__(self, callback, event_mask, obj_or_fd):
        self.callback = callback
        self.event_mask = event_mask
        self.obj = obj_or_fd


class PollService(object):
    DEFAULT_EMASK = (select.EPOLLIN | select.EPOLLOUT |
                     select.EPOLLERR | select.EPOLLHUP)
    READER_EMASK = (select.EPOLLIN | select.EPOLLERR |
                    select.EPOLLHUP)
    WRITER_EMASK = (select.EPOLLOUT | select.EPOLLERR |
                    select.EPOLLHUP)

    def __init__(self):
        self._ep = select.epoll()
        self._entries = dict()
        self._lock = threading.RLock()
        self._log = logging.getLogger("polld")
        self._eclose = EventFD(init_value=0)
        self._running = True
        self.add(self._eclose, self._closefd_cb, self.READER_EMASK)

    def _realclose(self):
        self._running = False
        self.remove(self._eclose.fileno())
        self._eclose.close()

    def _closefd_cb(self, pobj, fd, obj, event):
        if event & select.EPOLLIN:
            self._eclose.wait()
            self._realclose()
        if event & select.EPOLLERR:
            logging.debug("Error on closefd fd=%d" % fd)
        if event & select.EPOLLHUP:
            logging.debug("HUP on closefd fd=%d" % fd)

    def _get_fd(self, obj_or_fd):
        if isinstance(obj_or_fd, int):
            return obj_or_fd
        else:
            return obj_or_fd.fileno()

    def add(self, obj_or_fd, callback, event_mask=None):
        if event_mask is None:
            event_mask = self.DEFAULT_EMASK
        fd = self._get_fd(obj_or_fd)
        with self._lock:
            if fd in self._entries:
                raise AlreadyAssignedError()
            self._entries[fd] = PollEntry(callback,
                                          event_mask,
                                          obj_or_fd)
            self._ep.register(fd, event_mask)
            self._log.debug("Registered fd=%d" % fd)
            return fd

    def modify(self, obj_or_fd, callback=None, event_mask=None):
        fd = self._get_fd(obj_or_fd)
        with self._lock:
            if fd in self._entries:
                entry = self._entries[fd]
                if callback is not None:
                    entry.callback = callback
                    self._log.debug("Changed callback fd=%d" % fd)
                if event_mask is not None:
                    self._ep.modify(fd, event_mask)
                    self._log.debug(
                        "Changed event mask fd=%d old_events=%x, new_events=%x"
                        % (fd, entry.event_mask, event_mask)
                    )
                    entry.event_mask = event_mask
            else:
                raise BadFDError()

    def remove(self, obj_or_fd):
        fd = self._get_fd(obj_or_fd)
        with self._lock:
            if fd in self._entries:
                self._ep.unregister(fd)
                del self._entries[fd]

    def poll(self, timeout=-1, maxevents=-1):
        if not self._running:
            return False
        for fd, event in self._ep.poll(timeout=timeout,
                                       maxevents=maxevents):
            with self._lock:
                assert fd in self._entries, "only valid fd's"
                cb = self._entries[fd].callback
                obj = self._entries[fd].obj
            cb(self, fd, obj, event)
        if self._running:
            return True
        else:
            self._ep.close()
            return False

    def exit_loop(self):
        if self._running:
            self._eclose.notify()

    def close(self):
        self._ep.close()


class TTYUnit(object):
    def __init__(self, path):
        self._path = path
        self._fd = -1
        self._old_flags = 0

    def open(self):
        if self._fd != -1:
            self.close()
        self._fd = os.open(self._path, os.O_RDWR | os.O_NOCTTY |
                           os.O_NDELAY)
        self._set_block_mode(False)
        self._oldstate = termios.tcgetattr(self._fd)
        self._set_mode()

    def _set_block_mode(self, blocking):
        self._old_flags = set_block_mode(self._fd, blocking)

    def _restore_block_mode(self):
        fcntl.fcntl(self._fd, fcntl.F_SETFL, self._old_flags)

    def _set_mode(self):
        tty.setraw(self._fd, termios.TCSANOW)
        opt = termios.tcgetattr(self._fd)
        opt[termios.c_cflag] =  (termios.CS8 | termios.CLOCAL | termios.HUPCL)
        opt[termios.c_iflag] &= ~(termios.IGNBRK | termios.IXON |
                                  termios.IXOFF | termios.IXANY)
        opt[termios.c_lflag] = 0
        opt[termios.c_oflag] = 0
        opt[termios.c_cc][termios.VMIN] = 1
        opt[termios.c_cc][termios.VTIME] = 0
        opt[termios.ispeed] = termios.B1200
        opt[termios.ospeed] = termios.B1200
        termios.tcsetattr(self._fd, termios.TCSADRAIN, opt)

    def read(self, max=1000):
        try:
            return os.read(self._fd, max)
        except os.error:
            return ""

    def write(self, data):
        self._set_block_mode(True)
        try:
            return os.write(self._fd, data)
        finally:
            self._restore_block_mode()

    def write_nb(self, data):
        return os.write(self._fd, data)

    def close(self):
        if self._fd != -1:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._oldstate)
            except termios.error:
                logging.error("termios reset failed")
            os.close(self._fd)
            self._fd = -1

    def is_open(self):
        return (self._fd != -1)

    def fileno(self):
        return self._fd


def stream_to_nonblocking(stream):
    flags = fcntl.fcntl(stream, fcntl.F_GETFL)
    fcntl.fcntl(stream, fcntl.F_SETFL, flags | os.O_NONBLOCK)


class IRWatchFD(TTYUnit):
    RE_IR = re.compile(r'IR:\s+([0-9,a-f,A-F]+)#')
    RE_VOL = re.compile(r'VOL:([0-9,a-f,A-F]+)#')
    def __init__(self, poll_obj, path):
        TTYUnit.__init__(self, path)
        self._wbuf = ""
        self._poll = poll_obj
        self._close_cb = None
        self._event_cb = None

    def set_close_cb(self, close_cb=None):
        self._close_cb = close_cb

    def set_event_cb(self, event_cb=None):
        self._event_cb = event_cb

    def open(self):
        TTYUnit.open(self)
        self._poll.add(self, self._poll_callback, PollService.READER_EMASK)

    def _actual_write(self):
        wbuf_size = len(self._wbuf)
        if wbuf_size > 0:
            ret = os.write(self._fd, self._wbuf)
            if ret > 0:
                self._wbuf = self._wbuf[ret:wbuf_size]
        else:
            self._poll.modify(self, event_mask=PollService.READER_EMASK)

    def write(self, data):
        self._wbuf += data
        try:
            self._poll.modify(self, event_mask=PollService.DEFAULT_EMASK)
        except BadFDError:
            logging.error("Bad fd!")

    def write_nb(self, data):
        self.write(data)

    def _poll_callback(self, pobj, fd, obj, event):
        logging.debug("term_cb: Event %d on fd=%d" % (event, fd))
        if event & select.EPOLLIN:
            data = obj.read()
            logging.debug("Data fd=%d: %s" % (fd, data))
            for ev in self.RE_IR.finditer(data):
                ir_code = ev.group(1)
                logging.debug("Got IR = {0}".format(ir_code))
                if self._event_cb is not None:
                    self._event_cb("IR", ir_code)
            for ev in self.RE_VOL.finditer(data):
                vol = ev.group(1)
                logging.debug("Found volume {0}".format(vol))
                if self._event_cb is not None:
                    self._event_cb("VOL", vol)
        if event & select.EPOLLOUT:
            self._actual_write()
        if event & select.EPOLLERR:
            logging.debug("Error on fd=%d" % fd)
        if event & select.EPOLLHUP:
            logging.debug("HUP on fd=%d" % fd)
            self.close()
            if self._close_cb is not None:
                self._close_cb()

    def close(self):
        if self._fd != -1:
            self._poll.remove(self)
        TTYUnit.close(self)

class MPDInterface(object):
    RE_STAT = re.compile(r'^[[](paused|playing)[]]', re.MULTILINE)
    def __init__(self, path="/usr/bin/mpc"):
        self._bin = path
        self._dummy_mode = False
        if not os.path.exists(path):
            self._dummy_mode = True

    def _mpc(self, cmd):
        ret = 0
        cmd = list(cmd)
        cmd.insert(0, self._bin)
        cmd_str = " ".join(cmd)
        if self._dummy_mode:
            logging.info("Dummy mode command: [{0}]".format(cmd_str))
            return ret, ''
        else:
            output = ""
            try:
                output = subprocess.check_output(cmd)
            except subprocess.CalledProcessError as e:
                ret = e.returncode
            logging.debug("Call: '{0}': ret={1}".format(cmd_str, ret))
            return ret, output

    def _mpc_status(self):
        ret, out = self._mpc_call("status", '--format=""')
        if ret == 0:
            m = self.RE_STAT.search(out)
            if m is None:
                return 'stopped'
            else:
                return m.group(1)
        else:
            return 'unkown'

    def _mpc_call(self, *args):
        return self._mpc(args)

    def currentSong(self):
        ret, out = self._mpc_call("current")
        if (ret == 0) and (out != ''):
            return True, out
        else:
            return False, ''

    def play(self):
        status = self._mpc_status()
        if status == 'playing':
            return 0
        if status in ('unkown', 'stopped'):
            self.stop()
        return self.togglePlay()

    def pause(self):
        status = self._mpc_status()
        if status == 'playing':
            return self.togglePlay()
        else:
            return 0

    def togglePlay(self):
        return self._mpc_call("toggle")[0]

    def stop(self):
        return self._mpc_call("stop")[0]

    def next(self):
        return self._mpc_call("next")[0]

    def prev(self):
        return self._mpc_call("prev")[0]


def _bent(cmd, help, p=0, params=tuple()):
    """ Construct Bot command entry

    :param cmd:    Command to run
    :param help:   Help text to display
    :param p:      Priority for sorting; higher means output first on /help
    :param params: Tuple of parameter names for the command (for display only)
    """
    return dict( cmd = cmd
               , help = help
               , priority = p
               , parameter = params
               )


class MusicBot(threading.Thread):
    REQUIRED = ('NAME', 'TOKEN', 'PASSWORD', 'TIMEOUT', 'API_URL')
    def __init__(self, config_file, users_file='users.json', botif=None, welcome=True):
        threading.Thread.__init__(self)
        self.daemon = True  # kill on exit
        self._cfg = self._loadConfig(config_file)
        self._users_file = users_file
        self._loadUsers()
        self._saveUsers()
        self._if = botif
        self._welcome = welcome
        self._unpriv = \
                { '/start' : _bent( self._cmdStart
                                  , "Register to use this bot"
                                  , params=('key',)
                                  )
                , '/help'  : _bent(self._cmdHelp, "This help text")
                , '/cmd'   : _bent( self._cmdCommandList
                                  , "Output command list (for @BotFather /setcommands)"
                                  )
                }
        self._priv = \
                { '/stop'  : _bent(self._cmdStop, "Unregister")
                , '/quiet' : _bent( self._cmdQuiet
                                  , "Quiet mode: Reduce volume by -3dB"
                                  , p = 100
                                  )
                , '/louder' : _bent(self._cmdLouder, "Increase volume (+3dB)", p = 5)
                , '/info'  : _bent(self._cmdInfo, "Get current volume")
                , '/next'  : _bent( lambda *a: self._mpcSimple(a[0], 'next')
                                  , "Next song"
                                  , p = 5
                                  )
                , '/previous' : _bent( lambda *a: self._mpcSimple(a[0], 'previous')
                                     , "Previous song"
                                     , p = 5
                                     )
                , '/play' : _bent( lambda *a: self._mpcSimple(a[0], 'play')
                                 , "Start playback"
                                 , p = 5
                                 )
                , '/pause' : _bent( lambda *a: self._mpcSimple(a[0], 'pause')
                                  , "Pause playback"
                                  , p = 6
                                  )
                , '/off' : _bent( lambda *a: self._mpcSimple(a[0], 'stop')
                                , "Stop playback and switch off"
                                , p = 4
                                )
                , '/songname' : _bent(self._cmdGetSong, "Get current song", p=3)
                }

    def _all_cmds(self):
        for k,v in self._unpriv.items():
            yield (k,v)
        for k,v in self._priv.items():
            yield (k,v)

    def _priv_call(self, cmd, *args):
        d = self._priv[cmd]
        return d['cmd'](*args)

    def _unpriv_call(self, cmd, *args):
        d = self._unpriv[cmd]
        return d['cmd'](*args)

    def _request(self, method, obj, **kwargs):
        url = self._cfg['API_URL'].format(**self._cfg)
        try:
            r = requests.post(url + method, json=obj, stream=True, **kwargs)
            r = r.json()
            if 'ok' not in r:
                r['ok'] = False
            return r
        except Exception:
            txt = traceback.format_exc()
            logging.error("Failed to send request: {0}".format(txt))
            return dict(ok=False)

    def _loadUsers(self):
        self._users = set()
        try:
            with open(self._users_file, 'r') as f:
                self._users = set([ str(i) for i in json.load(f)])
            logging.debug("Load users: {0}".format(", ".join(self._users)))
        except Exception as e:
            txt = traceback.format_exc()
            logging.error("Failed to load users: {0}".format(txt))

    def _saveUsers(self):
        try:
            with open(self._users_file, 'w') as f:
                json.dump(list(self._users), f)
        except Exception as e:
            txt = traceback.format_exc()
            logging.error("Failed to load users: {0}".format(txt))

    def _sendTextMessage(self, chat_id, text):
        r = self._request( 'sendMessage'
                         , dict( chat_id = chat_id
                               , text = text
                               )
                         )
        if r['ok']:
            return True
        else:
            logging.warning("Failed to send text: {0}".format(r))
            return False

    def _sendAllTextMessage(self, text):
        ret = True
        for id in self._users:
            r = self._sendTextMessage(id, text)
            ret = (ret and r)
        return ret

    def _loadConfig(self, config_file):
        data = {}
        with open(config_file, 'r') as f:
            cfg = json.load(f)
        missing = set()
        for i in cfg.keys():
            if i not in self.REQUIRED:
                missing.add(i)
        if missing:
            raise RuntimeError("Missing required fields: {0}".format(", ".join(missing)))
        return cfg

    def _processMessage(self, msg):
        try:
            if msg['text'].startswith("/"):
                cmd = msg['text'].split(" ", 1)
                if "@" in cmd[0]:
                    _, bot = cmd[0].split("@", 1)
                    if bot.lower() != self._cfg['NAME']:
                        logging.debug("Ignore message to different bot: {0}".format(cmd))
                        return
                self._processCommand(msg, *cmd)
        except KeyError as e:
            tb = traceback.format_exc()
            logging.debug("Missing key: {0}\n{1}".format(e, tb))
        except Exception:
            txt = traceback.format_exc()
            logging.error("Failed to process message: {0}".format(txt))

    def _processCommand(self, msg, cmd, *param):
        cid = msg['chat']['id']
        registered = str(cid) in self._users
        unhandled = False
        param = " ".join(param)

        if cmd in self._unpriv.keys():
            self._unpriv_call(cmd, cid, param, msg)
        elif cmd in self._priv.keys():
            if registered:
                self._priv_call(cmd, cid, param, msg)
            else:
                self._sendTextMessage(cid, "Sign in using /start <key>")
        else:
            self._sendTextMessage(cid, "Unkown command: {0}".format(cmd))

    def _cmdStart(self, cid, param, msg):
        if param.strip() == self._cfg['PASSWORD']:
            self._users.add(str(cid))
            self._saveUsers()
            self._sendTextMessage(cid, "You are signed in.")
        else:
            self._sendTextMessage(cid, "Invalid password")
            self._sendAllTextMessage("Failed login attempt: cid={0}".format(cid))

    def _sort_cmds(self, item):
        return (-item[1]['priority'], item[0])

    def _cmdHelp(self, cid, param, msg):
        help = ["RPI Music Bot\n"]
        max_len = max([len(i[0]) for i in self._all_cmds()])
        for i in sorted(self._all_cmds(), key=self._sort_cmds):
            help.append("{0:<{max}} - {1[help]}".format(*i, max=max_len))
        self._sendTextMessage(cid, "\n".join(help))

    def _cmdCommandList(self, cid, param, msg):
        cmds = []
        max_len = max([len(i[0]) for i in self._all_cmds()]) - 1  # no '/'
        for i in sorted(self._all_cmds(), key=self._sort_cmds):
            c = i[0][1:]
            cmds.append("{cmd:<{max}} - {1[help]}".format(*i, max=max_len, cmd=c))
        self._sendTextMessage(cid, "\n".join(cmds))

    def _cmdStop(self, cid, param, msg):
        self._users.discard(str(cid))
        self._sendTextMessage(cid, "Successfully unregistered")

    def _cmdQuiet(self, cid, param, msg):
        notify = "{0} requested silence; be nice..."
        logging.debug("Quiet: {0}".format(msg))
        usr = ( msg['chat'].get('first_name', str(cid))
              + " "
              + msg['chat'].get('last_name', '')
              ).strip(" ")
        for i in self._users:
            if str(cid) != i:
                self._sendTextMessage( i
                                     , notify.format(usr)
                                     )
        # TODO: Silence
        if self._if is not None:
            self._if.sendQuestion("quiet")
            self._sendTextMessage(cid, "Is it better now?")

    def _cmdLouder(self, cid, param, msg):
        logging.debug("Increase volume +3dB")
        usr = ( msg['chat'].get('first_name', str(cid))
              + " "
              + msg['chat'].get('last_name', '')
              ).strip(" ")
        if self._if is not None:
            self._if.sendQuestion("louder")
            self._sendAllTextMessage("{0} increased volume".format(usr))
        else:
            self._sendTextMessage(cid, "Interface unavailable... :(")

    def _cmdInfo(self, cid, param, msg):
        if self._if is not None:
            repl = self._if.sendQuestion("volume-info")
            self._sendTextMessage(cid, repl)

    def _mpcSimple(self, cid, mpc_cmd):
        if self._if is not None:
            repl = self._if.sendQuestion(mpc_cmd)
            self._sendTextMessage(cid, repl)

    def _cmdGetSong(self, cid, param, msg):
        if self._if is not None:
            repl = self._if.sendQuestion("getSong")
            if repl == "":
                self._sendTextMessage(cid, "No song is currently playing")
            else:
                t = "Current song: {}".format(repl)
                self._sendTextMessage(cid, t)

    def run(self, *args, **kwargs):
        last_update_id = 0

        if self._welcome:
            self._sendAllTextMessage("Welcome")
        timeout = self._cfg['TIMEOUT']

        while True:
            try:
                r = self._request('getUpdates'
                                 , dict( offset = last_update_id + 1
                                       , timeout = timeout
                                       )
                                 , timeout = timeout + 5
                                 )

                if r['ok']:
                    for i in r['result']:
                        if i['update_id'] > last_update_id:
                            last_update_id = i['update_id']
                        self._processMessage(i['message'])
            except Exception:
                txt = traceback.format_exc()
                logging.error("Failed to fetch updates: {0}".format(txt))

    def exit(self):
        # TODO: Implement proper handling (interrupt request)
        logging.info("Music Bot is terminated")


class IRBotPlugin(object):
    def __init__(self):
        self.fd = EventFD()
        self._q = queue.Queue()
        self._a = queue.Queue()

    def sendQuestion(self, obj):  # to app
        self._q.put(obj)
        self.fd.notify()
        return self._a.get(timeout=20)

    def getQuestion(self):
        self.fd.wait()
        return self._q.get(timeout=20)

    def sendReply(self, obj):
        self._a.put(obj)

    def fileno(self):
        return self.fd.fileno()


class IRApp(object):
    _DEBOUNCE_NS = 400000000

    def __init__(self, path='/dev/ttyIRUSB'):
        self._dev_path = path
        self._exit_cmd = ""
        self._poll = PollService()
        self._mpd = MPDInterface()
        self._watch = IRWatchFD(self._poll, path)
        self._watch.set_close_cb(self._disconnect)
        self._watch.set_event_cb(self._watch_callback)
        stream_to_nonblocking(sys.stdin)
        self._poll.add(sys.stdin, self._stdin_callback,
                       PollService.READER_EMASK)
        # Bot
        self.botif = IRBotPlugin()
        self._poll.add(self.botif, self._bot_callback,
                       PollService.READER_EMASK)
        # scan timerfd
        self._scan_tfd = TimerFD()
        self._poll.add(self._scan_tfd, self._scan_callback,
                       PollService.READER_EMASK)
        self._enable_scan()
        # debounce timerfd (IR)
        self._last_ir = "" # unlocked
        self._debounce_tfd = TimerFD()
        self._poll.add(self._debounce_tfd, self._unlock_last_ir_cb,
                       PollService.READER_EMASK)
        # start operation
        self._connect()

    def _enable_scan(self):
        logging.info("Enable scanning ...")
        self._scan_tfd.set_rtime(5, repeated=True) # scan all 5 seconds for ttyIRUSB

    def _unlock_last_ir_cb(self, pobj, fd, obj, event):
        logging.debug("Event %d on fd=%d" % (event, fd))
        if event & select.EPOLLIN:
            ret = obj.wait()
            self._last_ir = ""
            logging.debug("Timer expired %d: Unlock locked ir code" % ret)
        if event & select.EPOLLERR:
            logging.debug("Error on fd=%d" % fd)
        if event & select.EPOLLHUP:
            logging.debug("HUP on fd=%d" % fd)

    def _connect(self):
        logging.debug("Trying to connect")
        try:
            self._watch.open()
            self._scan_tfd.disarm()
        except Exception as e:
            logging.error("Connecting failed: %s" % str(e))

    def _disconnect(self):
        "Event to indicate that IRWatch has been disconnected"
        logging.debug("Disconnect IRWatch")
        self._enable_scan()

    def _set_power(self, on=False):
        if self._watch.is_open():
            if on:
                logging.debug("Enable power relay")
                self._watch.write('P')
            else:
                logging.debug("Disable power relay")
                self._watch.write('p')
        else:
            logging.debug("Arduino not connected")

    def _watch_callback(self, t, ev):
        if t == "IR":
            self._ir_code_cb(ev)
        elif t == "VOL":
            if self.botif is not None:
                txt = "Volume is {}".format(ev)
                self.botif.sendReply(txt)

    def _ir_code_cb(self, ir_code):
        IR_PLAY = "a659758a"
        IR_NEXT = "a659916e"
        IR_STOP = "a659906f"
        IR_PREV = "a659926d"

        logging.debug("IR callback: {0}".format(ir_code))

        if ir_code == self._last_ir:
            logging.debug("Same IR code as last time: {0}".format(ir_code))
            return # debounce

        self._last_ir = ir_code
        self._debounce_tfd.set_rtime(0, nanosec=self._DEBOUNCE_NS,
                                     repeated=False)
        if ir_code == IR_PLAY:
            logging.info("Play / Pause ({0})".format(ir_code))
            self._mpd.togglePlay()
            self._set_power(on=True)
        elif ir_code == IR_STOP:
            logging.info("Stop ({0})".format(ir_code))
            self._mpd.stop()
            self._set_power(on=False)
        elif ir_code == IR_NEXT:
            logging.info("Next >>| ({0})".format(ir_code))
            self._mpd.next()
        elif ir_code == IR_PREV:
            logging.info("Previous |<< ({0})".format(ir_code))
            self._mpd.prev()
        elif ir_code != "":
            logging.debug("Unimplemented IR code: {0}".format(ir_code))

    def _scan_callback(self, pobj, fd, obj, event):
        logging.debug("Event %d on fd=%d" % (event, fd))
        if event & select.EPOLLIN:
            ret = obj.wait()
            logging.debug("Timer expired %d" % ret)
            if not self._watch.is_open() and os.path.exists(self._dev_path):
                self._connect()
        if event & select.EPOLLERR:
            logging.debug("Error on fd=%d" % fd)
        if event & select.EPOLLHUP:
            logging.debug("HUP on fd=%d" % fd)

    def _bot_callback(self, pobj, fd, obj, event):
        logging.debug("Event %d on fd=%d" % (event, fd))
        if event & select.EPOLLIN:
            data = obj.getQuestion()
            logging.debug("Received {0}".format(data))
            if data in ("t", "toggle"):
                self._mpd.togglePlay()
                self._set_power(on=True)
                obj.sendReply("OK")
            elif data == "play":
                self._mpd.play()
                self._set_power(on=True)
                obj.sendReply("OK")
            elif data == "pause":
                self._mpd.pause()
                obj.sendReply("OK")
            elif data == "getSong":
                st, song = self._mpd.currentSong()
                if st:
                    obj.sendReply(song)
                else:
                    obj.sendReply("")
            elif data in ("s", "stop"):
                self._mpd.stop()
                self._set_power(on=False)
                obj.sendReply("OK")
            elif data in ("n", "next"):
                self._mpd.next()
                obj.sendReply("OK")
            elif data in ("p", "prev", "previous"):
                self._mpd.prev()
                obj.sendReply("OK")
            else: # Arduino commands:
                if not self._watch.is_open():
                    obj.sendReply("Music volume control board is not connected/powered; Sorry :(")
                    return # exit
                elif data in ("q", "quiet", "quiet-mode"):
                    self._watch.write("Q")
                    obj.sendReply("OK")
                elif data in ("unlock",):
                    self._watch.write("U")
                    obj.sendReply("OK")
                elif data in ("info",):
                    self._watch.write("i")
                elif data in ("volume", "volume-info"):
                    self._watch.write("I")
                elif data in ("louder", "L"):
                    self._watch.write("L")
                    obj.sendReply("OK")
                else:
                    obj.sendReply("Failed")

    def _stdin_callback(self, pobj, fd, obj, event):
        logging.debug("Event %d on fd=%d" % (event, fd))
        if event & select.EPOLLIN:
            data = obj.read(100).strip("\n")
            logging.debug("Data fd=%d: %s" % (fd, data))
            if data == "close":
                self._poll.exit_loop()
            elif data == "connect":
                logging.debug("Try to connect to arduino board")
                self._connect()
            elif data == "play":
                self._mpd.play()
                self._set_power(on=True)
            elif data in ("current", "song", "current song", "now", "playing"):
                st, song = self._mpd.currentSong()
                if st:
                    logging.info("Current song: {0}".format(song))
            elif data in ("t", "toggle"):
                self._mpd.togglePlay()
                self._set_power(on=True)
            elif data in ("s", "stop"):
                self._mpd.stop()
                self._set_power(on=False)
            elif data in ("n", "next"):
                self._mpd.next()
            elif data in ("p", "prev", "previous"):
                self._mpd.prev()
            elif data in ("loader", "bootloader"):
                self._watch.write('b')
            elif data.startswith('write'):
                cmd = data[5:].strip(" ")
                if len(cmd) > 0:
                    logging.info("Send command: {0}".format(cmd))
                    self._watch.write(cmd)
                else:
                    logging.debug("No command specified - ignoring")
            elif data == "help":
                logging.debug("Command List: %s" %
                              ['close', 'connect', 'toggle', 'stop',
                               'next', 'previous', 'bootloader'])
        if event & select.EPOLLERR:
            logging.debug("Error on fd=%d" % fd)
        if event & select.EPOLLHUP:
            logging.debug("HUP on fd=%d" % fd)

    def poll_main(self):
        ret = True
        try:
            while ret:
                ret = self._poll.poll()
        except KeyboardInterrupt:
            logging.debug("Quit application due to SIGINT")
        finally:
            self._watch.close()
            self._scan_tfd.close()
            self._poll.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument( '--verbose'
                       , dest = 'verbosity'
                       , action = 'store_const'
                       , const = logging.DEBUG
                       )
    parser.add_argument( '--quiet'
                       , dest = 'verbosity'
                       , action = 'store_const'
                       , const = logging.ERROR
                       )
    parser.add_argument( '--bot-config'
                       , dest = 'bot_cfg'
                       , default = None
                       )
    parser.add_argument( '--no-welcome'
                       , dest = 'welcome'
                       , default = True
                       , action = "store_false"
                       )
    parser.set_defaults(verbosity=logging.INFO)
    args = parser.parse_args()

    logging.basicConfig(stream=sys.stderr, format=_DEFAULT_LOG_FORMAT,
                        level=args.verbosity)

    music_bot = None
    DEV='/dev/ttyIRUSB'
    dut = IRApp(DEV)
    if args.bot_cfg is not None:
        try:
            music_bot = MusicBot( args.bot_cfg
                                , botif=dut.botif
                                , welcome=args.welcome
                                )
            music_bot.start()
        except RuntimeError:
            pass
    dut.poll_main()
    logging.info("Exit main")
    if music_bot is not None:
        music_bot.exit()
    logging.info("Exit program")

if __name__ == '__main__':
    main()
