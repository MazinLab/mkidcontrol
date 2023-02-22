#This is trying to recreate the funcitonality of Roach2Controls used by
#dashboard.py in mkidreadout for python3 

import binascii
import os
import time
import datetime
import calendar
from socket import inet_aton
import socket
import re
import struct
import numbers
import builtins

from logging import getLogger

LOGGER = getLogger(__name__)


class KatcpSyntaxError(ValueError):
    """Raised by parsers when encountering a syntax error."""


class Roach2Controls(object):
    def __init__(self, ip, paramFile='', num=None, verbose=False, debug=False):
        """
        Input:
            ip - ip address string of ROACH2
            paramFile - param object or directory string to dictionary containing important info
            verbose - show print statements
            debug - Save some things to disk for debugging
        """
        # np.random.seed(1) #Make the random phase values always the same
        self.verbose = verbose
        self.debug = debug
        if num is None:
            self.num = int(ip.split('.')[-1])
        else:
            self.num = num

        self.ip = ip
        #try:
        paramFile = paramFile if paramFile else os.path.join(os.path.dirname(__file__), 'darknessfpga.param')
        self.read_param_file(paramFile)
        #    getLogger(__name__).info('Loading params from {}'.format(paramFile))
        #    self.params = ReadDict(file=paramFile)
        #except TypeError:
        #self.params = paramFile

        if debug and not os.path.exists(self.params['debugDir']):
            os.makedirs(self.params['debugDir'])

    def read_param_file(self, filename):
        params = {}
        with open(filename, 'r') as f:
            old = ''
            for line in f:
                line = line.strip()
                if len(line) == 0 or line[0] == '#':
                    continue
                s = line.split('#')
                line = s[0]
                s = line.split('\\')
                if len(s) > 1:
                    old = ''.join([old, s[0]])
                    continue
                else:
                    line = ''.join([old, s[0]])
                    old = ''
                for i in range(len(line)):
                    if line[i] != ' ':
                        line = line[i:]
                        break
                #exec (line)
                s = line.split('=')
                if len(s) != 2:
                    getLogger(__name__).warning("Error parsing line:\n\t'{}'".format(line))
                    continue
                key = s[0].strip()
                val = eval(s[1].strip())  # XXX:make safer
                params[key] = val
        self.params = params

    def connect(self):
        self.fpga = MinimalRoachConnection(self.ip, timeout=3.)
        self.fpga.connect()
        time.sleep(.1)
        self.fpga._timeout = 50.
        if not self.fpga.is_running():
            getLogger(__name__).error('Firmware is not running. Start firmware, calibrate, '
                                      'and load wave into qdr first!')
            return False
        else:
            self.fpga.get_system_information()
            return True

    def setPhotonCapturePort(self, port):
        self.fpga.write_int(self.params['photonPort_reg'], int(port))

    def loadCurTimestamp(self):
        """
        Loads current time, in seconds since Jan 1 00:00 UTC this year
        """
        timestamp = int(time.time())
        curYr = datetime.datetime.utcnow().year
        yrStart = datetime.date(curYr, 1, 1)
        tsOffs = calendar.timegm(yrStart.timetuple())
        timestamp -= tsOffs
        self.fpga.write_int(self.params['timestamp_reg'], timestamp)

    def stopSendingPhotons(self):
        self.fpga.write_int(self.params['photonCapStart_reg'], 0)

    def startSendingPhotons(self, dest_ip, port):
        dest_ip = binascii.hexlify(inet_aton(dest_ip))

        self.fpga.write_int(self.params['destIP_reg'], int(dest_ip, 16))
        self.fpga.write_int(self.params['photonPort_reg'], int(port))
        self.fpga.write_int(self.params['wordsPerFrame_reg'], int(self.params['wordsPerFrame']))

        # restart gbe
        self.fpga.write_int(self.params['photonCapStart_reg'], 0)
        self.fpga.write_int(self.params['phaseDumpEn_reg'], 0)
        self.fpga.write_int(self.params['gbe64Rst_reg'], 1)
        time.sleep(.01)
        self.fpga.write_int(self.params['gbe64Rst_reg'], 0)

        # Start
        self.fpga.write_int(self.params['photonCapStart_reg'], 1)

    def setMaxCountRate(self, cpsLimit=2500):
        for reg in self.params['captureCPSlim_regs']:
            try:
                self.fpga.write_int(reg, cpsLimit)
            except:
                getLogger(__name__).error("Couldn't write to %s", reg)



class MinimalRoachConnection():
    def __init__(self, ip, port=7147, timeout=3.):
        self._stream = None
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.max_msg = 20 * 1024 * 1024 #20MB
        self.max_buffer = 200 * 1024 * 1024 #200MB
        self.roach_num = ip.split('.')[-1]
        #max message and buffer sizes are what they are set to in the Mazin lab
        #version of CasperFpga, though for these operations, that might be
        #overkill

    def connect(self):
        #I think we will do things like dashboard (serially iterate over the
        #roaches), so even though I am still using tornado async/thread safe stuff
        #I think I can eschew some of the thread id checks
        if self._stream:
            self.disconnect()
        self._stream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._stream.connect((self.ip, self.port))

    def disconnect(self):
        if self._stream:
            self._stream.close()
        self._stream = None

    def send(self, msg, timeout=None):
        if not self._stream:
            self.connect()
        sent_size = self._stream.send(msg)
        if sent_size == 0:
            raise RuntimeError('Connection to ROACH {0} has died'.format(
                    self.roach_num))
        return None, None

    def is_running(self):
        if self._stream:
            return True
        else:
            return False

    def katcprequest(self, name, request_timeout=-1.0,
                     request_args=()):
        """
        Make a non-blocking request to the KATCP server and check the result.
        :param name: request message to send.
        :param request_timeout: number of seconds after which the request
        must time out
        :param request_args: request arguments.
        :return: tuple of reply and informs
        """
        # TODO raise sensible errors
        if request_timeout == -1:
            request_timeout = self._timeout
        request = Message.request(name, *request_args)
        reply, informs = self.send(request.byte_message() + b'\n',
                                   timeout=request_timeout)
        #if (reply.arguments[0] != katcp.Message.OK) and require_ok:
        #    if reply.arguments[0] == katcp.Message.FAIL:
        #        raise KatcpRequestFail(
        #            'Request %s on host %s failed.\n\t'
        #            'Request: %s\n\tReply: %s' %
        #            (request.name, self.host, request, reply))
        #    elif reply.arguments[0] == katcp.Message.INVALID:
        #        raise KatcpRequestInvalid(
        #            'Invalid katcp request %s on host %s.\n\t'
        #            'Request: %s\n\tReply: %s' %
        #            (request.name, self.host, request, reply))
        #    else:
        #        raise KatcpRequestError(
        #            'Unknown error processing request %s on host '
        #            '%s.\n\tRequest: %s\n\tReply: %s' %
        #            (request.name, self.host, request, reply))
        return reply, informs

    def write_int(self, device_name, integer, word_offset=0):
        """
        Writes an integer to the device specified at the offset specified.
        A blind write is now enforced.
        :param device_name: device to be written
        :param integer: the integer to write
        :param word_offset: the offset at which to write, in 32-bit words
        :return:
        """
        # careful of packing input data into 32 bit - check range: if
        # negative, must be signed int; if positive over 2^16, must be unsigned
        # int.
        try:
            data = struct.pack('>i' if integer < 0 else '>I', integer)
        except Exception as ve:
            LOGGER.error('Writing integer %i failed with error: %s' % (
                integer, ve.message))
            raise ValueError('Writing integer %i failed with error: %s' % (
                integer, ve.message))
        self.blindwrite(device_name, data, word_offset*4)
        LOGGER.debug('%s: write_int %8x to register %s at word offset %d '
                     'okay%s.' % (self.ip, integer, device_name,
                                  word_offset,
                                  ' (blind)'))

    def blindwrite(self, device_name, data, offset=0):
        """
        Unchecked data write.
        :param device_name: the memory device to which to write
        :param data: the byte string to write
        :param offset: the offset, in bytes, at which to write
        :return: <nothing>
        """
        #assert(type(data) == str), 'You need to supply binary packed ' \
                #                           'string data!'
        assert(len(data) % 4) == 0, 'You must write 32-bit-bounded words!'
        assert((offset % 4) == 0), 'You must write 32-bit-bounded words!'
        self.katcprequest(name='write', request_timeout=self._timeout,
                          request_args=(device_name, str(offset), data))


class Message(object):
    """Represents a KAT device control language message.
    copied from katcp.core

    Parameters
    ----------
    mtype : Message type constant
        The message type (request, reply or inform).
    name : str
        The message name.
    arguments : list of objects (float, int, bool, bytes, or str)
        The message arguments.
    mid : str or bytes (digits only), int, or None
        The message identifier. Replies and informs that
        are part of the reply to a request should have the
        same id as the request did.

    """
    # Message types
    REQUEST, REPLY, INFORM = range(3)

    # Reply codes
    # TODO: make use of reply codes in device client and server
    OK, FAIL, INVALID = b"ok", b"fail", b"invalid"

    ## @brief Mapping from message type to string name for the type.
    TYPE_NAMES = {
        REQUEST: "REQUEST",
        REPLY: "REPLY",
        INFORM: "INFORM",
    }

    ## @brief Mapping from message type to type code character.
    TYPE_SYMBOLS = {
        REQUEST: b"?",
        REPLY: b"!",
        INFORM: b"#",
    }

    # pylint fails to realise TYPE_SYMBOLS is defined
    # pylint: disable-msg = E0602

    ## @brief Mapping from type code character to message type.
    TYPE_SYMBOL_LOOKUP = dict((v, k) for k, v in TYPE_SYMBOLS.items())

    # pylint: enable-msg = E0602

    ## @brief Mapping from escape character to corresponding unescaped string.
    ESCAPE_LOOKUP = {
        b"\\": b"\\",
        b"_": b" ",
        b"0": b"\0",
        b"n": b"\n",
        b"r": b"\r",
        b"e": b"\x1b",
        b"t": b"\t",
        b"@": b"",
    }

    # pylint fails to realise ESCAPE_LOOKUP is defined
    # pylint: disable-msg = E0602

    ## @brief Mapping from unescaped string to corresponding escape character.
    REVERSE_ESCAPE_LOOKUP = dict((v, k) for k, v in ESCAPE_LOOKUP.items())

    # pylint: enable-msg = E0602

    ## @brief Regular expression matching all unescaped character.
    ESCAPE_RE = re.compile(br"[\\ \0\n\r\x1b\t]")

    ## @var mtype
    # @brief Message type.

    ## @var name
    # @brief Message name.

    ## @var arguments
    # @brief List of string message arguments.

    ## @brief Attempt to optimize messages by specifying attributes up front
    __slots__ = ["mtype", "name", "mid", "arguments"]

    def __init__(self, mtype, name, arguments=None, mid=None):
        self.mtype = mtype
        self.name = name

        if mid is None:
            self.mid = None
        else:
            if not isinstance(mid, builtins.bytes):
                self.mid = str(mid).encode('ascii')
            else:
                self.mid = mid

        if arguments is None:
            self.arguments = []
        else:
            self.arguments = [self.format_argument(x) for x in arguments]

        # check message type

        if mtype not in self.TYPE_SYMBOLS:
            raise KatcpSyntaxError("Invalid command type %r." % (mtype,))

        # check message id

        if self.mid is not None and not self.mid.isdigit():
            raise KatcpSyntaxError("Invalid message id %r." % (mid,))

        # check command name validity

        if not name:
            raise KatcpSyntaxError("Command missing command name.")
        if not name.replace("-", "").isalnum():
            raise KatcpSyntaxError("Command name should consist only of "
                                   "alphanumeric characters and dashes (got %r)."
                                   % (name,))
        if not name[0].isalpha():
            raise KatcpSyntaxError("Command name should start with an "
                                   "alphabetic character (got %r)."
                                   % (name,))

    def format_argument(self, arg):
        """Format a Message argument to a byte string"""
        if isinstance(arg, bool):
            return b'1' if arg else b'0'
        elif isinstance(arg, int):
            return b'%d' % arg
        elif isinstance(arg, float):
            return repr(arg).encode('ascii')
        elif isinstance(arg, builtins.bytes):
            return arg
        elif isinstance(arg, builtins.str):
            return arg.encode('utf-8')
        # Note: checks for Integral and Real allow for numpy types,
        # but checks are quite slow, so do them as late as possible
        elif isinstance(arg, numbers.Integral):
            return b'%d' % arg
        elif isinstance(arg, numbers.Real):
            return repr(arg).encode('ascii')
        else:
            return arg.encode('utf-8')

    def copy(self):
        """Return a shallow copy of the message object and its arguments.

        Returns
        -------
        msg : Message
            A copy of the message object.

        """
        return Message(self.mtype, self.name, self.arguments)

    def __bytes__(self):
        """Return Message serialized for transmission.

        Returns
        -------
        msg : bytes
           The raw bytes of the serialised message, excluding terminating newline.

        """
        if self.arguments:
            escaped_args = [self.ESCAPE_RE.sub(self._escape_match, x)
                            for x in self.arguments]
            escaped_args = [x or b"\\@" for x in escaped_args]
            arg_str = b" " + b" ".join(escaped_args)
        else:
            arg_str = b""

        if self.mid is not None:
            mid_str = b"[%s]" % self.mid
        else:
            mid_str = b""

        return b"%s%s%s%s" % (self.TYPE_SYMBOLS[self.mtype], self.name.encode('utf-8'),
                              mid_str, arg_str)

    def byte_message(self):
        """Gives the message in bytes. This is added to the katcp implementation
        to avoid some of the issues with python3 changes to str/byte encoding
        and decoding

        Returns
        -------
        byte_str : bytes
            The raw bytes of the serialised message
        """
        byte_str = self.__bytes__()
        return byte_str

    def __str__(self):
        """Return Message serialized for transmission as native string.

        Returns
        -------
        msg : byte string in PY2, unicode string in PY3
           - In PY2, this string of bytes can be transmitted on the wire.
           - In PY3, the native string type is unicode, so it is decoded
             first using "utf-8" encoding.  See the warning in the next
             section.

        Raises
        ------
        UnicodeDecodeError:
            Under PY3, if the raw byte string cannot be decoded using UTF-8.
            Warning:  arbitrary bytes will not generally comply with the UTF-8
            encoding requirements, so rather use the `__bytes__` method,
            i.e., `bytes(msg)` instead of `str(msg)`.

        """
        byte_str = self.__bytes__()
        return byte_str.decode('utf-8')
        #except Exception as e:
        #    print(byte_str)
        #    raise e

    def __repr__(self):
        """Return message displayed in a readable form."""
        tp = self.TYPE_NAMES[self.mtype].lower()
        name = self.name
        if self.arguments:
            escaped_args = []
            for arg in self.arguments:
                escaped_arg = self.ESCAPE_RE.sub(self._escape_match, arg)
                if len(escaped_arg) > 1000:
                    escaped_arg = escaped_arg[:1000] + b"..."
                escaped_args.append(str(escaped_arg))
            args = "(" + ", ".join(escaped_args) + ")"
        else:
            args = ""
        return "<Message %s %s %s>" % (tp, name, args)

    def __eq__(self, other):
        if not isinstance(other, Message):
            return NotImplemented
        for name in self.__slots__:
            if getattr(self, name) != getattr(other, name):
                return False
        return True

    def __ne__(self, other):
        return not self == other

    def _escape_match(self, match):
        """Given a re.Match object, return the escape code for it."""
        return b"\\" + self.REVERSE_ESCAPE_LOOKUP[match.group()]

    def reply_ok(self):
        """Return True if this is a reply and its first argument is 'ok'."""
        return (self.mtype == self.REPLY and self.arguments and
                self.arguments[0] == self.OK)

    # * and ** magic useful here
    # pylint: disable-msg = W0142

    @classmethod
    def request(cls, name, *args, **kwargs):
        """Helper method for creating request messages.

        Parameters
        ----------
        name : str
            The name of the message.
        args : list of objects (float, int, bool, bytes, or str)
            The message arguments.

        Keyword arguments
        -----------------
        mid : str or bytes (digits only), int, or None
            Message ID to use or None (default) for no Message ID

        """
        mid = kwargs.pop('mid', None)
        if len(kwargs) > 0:
            raise TypeError('Invalid keyword argument(s): %r' % kwargs)
        return cls(cls.REQUEST, name, args, mid)

    @classmethod
    def reply(cls, name, *args, **kwargs):
        """Helper method for creating reply messages.

        Parameters
        ----------
        name : str
            The name of the message.
        args : list of objects (float, int, bool, bytes, or str)
            The message arguments.

        Keyword Arguments
        -----------------
        mid : str or bytes (digits only), int, or None
            Message ID to use or None (default) for no Message ID

        """
        mid = kwargs.pop('mid', None)
        if len(kwargs) > 0:
            raise TypeError('Invalid keyword argument(s): %r' % kwargs)
        return cls(cls.REPLY, name, args, mid)

    @classmethod
    def reply_to_request(cls, req_msg, *args):
        """Helper method for creating reply messages to a specific request.

        Copies the message name and message identifier from request message.

        Parameters
        ----------
        req_msg : katcp.core.Message instance
            The request message that this inform if in reply to
        args : list of objects (float, int, bool, bytes, or str)
            The message arguments.

        """
        return cls(cls.REPLY, req_msg.name, args, req_msg.mid)

    @classmethod
    def inform(cls, name, *args, **kwargs):
        """Helper method for creating inform messages.

        Parameters
        ----------
        name : str
            The name of the message.
        args : list of objects (float, int, bool, bytes, or str)
            The message arguments.

        Keyword Arguments
        -----------------
        mid : str or bytes (digits only), int, or None
            Message ID to use or None (default) for no Message ID

        """
        mid = kwargs.pop('mid', None)
        if len(kwargs) > 0:
            raise TypeError('Invalid keyword argument(s): %r' % kwargs)
        return cls(cls.INFORM, name, args, mid)

    @classmethod
    def reply_inform(cls, req_msg, *args):
        """Helper method for creating inform messages in reply to a request.

        Copies the message name and message identifier from request message.

        Parameters
        ----------
        req_msg : katcp.core.Message instance
            The request message that this inform if in reply to
        args : list of objects (float, int, bool, bytes, or str)
            The message arguments except name

        """
        return cls(cls.INFORM, req_msg.name, args, req_msg.mid)

    # pylint: enable-msg = W0142



