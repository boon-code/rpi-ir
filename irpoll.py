import sys
import os
import re
import tty
import time
import fcntl
import socket
import select
import errno
import struct
import logging
import threading
import ctypes
import termios
import optparse
import subprocess

termios.c_iflag = 0
termios.c_oflag = 1
termios.c_cflag = 2
termios.c_lflag = 3
termios.ispeed = 4
termios.ospeed = 5
termios.c_cc = 6

_DEFAULT_LOG_FORMAT = "%(name)s : %(threadName)s : %(levelname)s \
: %(message)s"

logging.basicConfig(stream=sys.stderr, format=_DEFAULT_LOG_FORMAT,
                    level=logging.INFO)

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
            if self._entries.has_key(fd):
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
            if self._entries.has_key(fd):
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
            if self._entries.has_key(fd):
                self._ep.unregister(fd)
                del self._entries[fd]

    def poll(self, timeout=-1, maxevents=-1):
        if not self._running:
            return False
        for fd, event in self._ep.poll(timeout=timeout,
                                       maxevents=maxevents):
            with self._lock:
                assert self._entries.has_key(fd), "only valid fd's"
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
        flags = fcntl.fcntl(self._fd, fcntl.F_GETFL, 0)
        self._old_flags = flags
        if blocking:
            flags &= ~os.O_NONBLOCK
        else:
            flags |= os.O_NONBLOCK
        fcntl.fcntl(self._fd, fcntl.F_SETFL, flags)

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
                    self._event_cb(ir_code)
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
    def __init__(self, path="/usr/bin/mpc"):
        self._bin = path
        self._dummy_mode = False
        self._output = ""
        if not os.path.exists(path):
            self._dummy_mode = True

    def _mpc(self, cmd):
        cmd.insert(0, self._bin)
        cmd_str = " ".join(cmd)
        if self._dummy_mode:
            logging.info("Dummy mode command: [{0}]".format(cmd_str))
        else:
            ret = 0
            try:
                self._output = subprocess.check_output(cmd)
            except subprocess.CalledProcessError as e:
                ret = e.returncode
            logging.debug("Call: '{0}': ret={1}".format(cmd_str, ret))
            return ret

    def _mpc_call(self, *args):
        return self._mpc(args)

    def togglePlay(self):
        return self._mpc_call("toggle")

    def stop(self):
        return self._mpc_call("stop")

    def forward(self):
        return self._mpc_call("next")

    def reverse(self):
        return self._mpc_call("prev")


class IRApp(object):
    _DEBOUNCE_NS = 400000000

    def __init__(self, path='/dev/ttyIRUSB'):
        self._dev_path = path
        self._exit_cmd = ""
        self._poll = PollService()
        self._mpd = MPDInterface()
        self._watch = IRWatchFD(self._poll, path)
        self._watch.set_close_cb(self._disconnect)
        self._watch.set_event_cb(self._ir_code_cb)
#        stream_to_nonblocking(sys.stdin)
#        self._poll.add(sys.stdin, self._poll_callback,
#                       PollService.READER_EMASK)
        # scan timerfd
        self._scan_tfd = TimerFD()
        self._poll.add(self._scan_tfd, self._scan_callback,
                       PollService.READER_EMASK)
        self._scan_tfd.set_rtime(20, repeated=True) # 20 seconds
        # debounce timerfd (IR)
        self._last_ir = "" # unlocked
        self._debounce_tfd = TimerFD()
        self._poll.add(self._debounce_tfd, self._unlock_last_ir_cb,
                       PollService.READER_EMASK)
        # start operation
        self._connect()

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
        except Exception as e:
            logging.error("Connecting failed: %s" % str(e))

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
            mpd.togglePlay()
        elif ir_code == IR_STOP:
            logging.info("Stop ({0})".format(ir_code))
            mpd.stop()
        elif ir_code == IR_NEXT:
            logging.info("Next >>| ({0})".format(ir_code))
            mpd.next()
        elif ir_code == IR_PREV:
            logging.info("Previous |<< ({0})".format(ir_code))
            mpd.prev()
        elif ir_code != "":
            logging.debug("Unimplemented IR code: {0}".format(ir_code))

    def _disconnect(self):
        "Event to indicate that IRWatch has been disconnected"
        logging.debug("Disconnect IRWatch")

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

    def _poll_callback(self, pobj, fd, obj, event):
        logging.debug("Event %d on fd=%d" % (event, fd))
        if event & select.EPOLLIN:
            data = obj.read(100).strip("\n")
            logging.debug("Data fd=%d: %s" % (fd, data))
            self._exit_cmd = ""
            m_goodn = self.RE_GOODN.match(data)
            if data == "close":
                logging.debug("Found close command!")
                self._poll.exit_loop()
            elif data == "connect":
                logging.debug("Try to connect to arduino board")
                self._connect()
            elif data == "wdog":
                logging.debug("Reset watchdog")
                self._watch.write("r")
            elif data == "info":
                logging.debug("Info request")
                self._watch.write("i")
            elif data == "shutdown":
                logging.debug("Shutdown rpi")
                self._watch.write("s")
                self._exit_cmd = "shutdown"
            elif data == "config_slow":
                logging.debug("Config shutdown to 10 Minutes")
                self._watch.write("c75\n")
            elif data == "config_fast":
                logging.debug("Config shutdown to 16 Seconds")
                self._watch.write("c1\n")
            elif data == "bootloader":
                logging.debug("Enter bootloader")
                self._watch.write("b")
            elif data == "btrigger":
                self._watch.write("t")
            elif data == "nimmer-pwr":
                logging.debug("Assert Nimmer PWR Button")
                self._watch.write("n")
            elif data == "nimmer-kill":
                logging.debug("Kill nimmer (force shutdown)")
                self._watch.write("k")
            elif data == 'nimmer':
                logging.debug("Nimmer status request")
                self._watch.write("p")
            elif data == 'led':
                logging.debug("Toggle nimmer LED status")
                self._watch.write("l")
            elif data == 'toggle-wdog':
                logging.debug("Toggle wdog (enabled/disabled)")
                self._watch.write("d")
            elif data == 'echo':
                logging.debug("Echo Request")
                self._watch.write("e")
            elif data == "help":
                logging.debug("Command List: %s" %
                              ['close', 'connect', 'info', 'shutdown',
                               'config_slow', 'config_fast', 'bootloader',
                               'help', 'good-night', 'btrigger', 'led',
                               'nimmer', 'nimmer-pwr', 'nimmer-kill',
                               'wdog', 'toggle-wdog', 'echo'])
            elif m_goodn is not None:
                hours = int(m_goodn.group(1))
                logging.debug("Good Night for %d hours" % hours)
                self._watch.write("c%d\ns" % (hours * 3600/8))
                self._exit_cmd = "shutdown"
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
    DEV='/dev/ttyIRUSB'
    dut = IRApp(DEV)
    dut.poll_main()
    logging.info("Exit program")

if __name__ == '__main__':
    main()
