"""
Author: Noah Swimmer 29 June 2020

A wrapper class to conveniently use redis-py and redistimeseries with PICTURE-C. This includes but is not limited to
inter-program communication (using pubsub), information storage (of device settings), and data storage (thermometry,
current, etc.).

TODO: Read TS_KEYS in the class?
Note there are 2 ways to read TS_KEYS.
 - (1) redis_timeseries.get(key) returns the last value in the timeseries (with timestamp)
 - (2) redis_timeseries.range(key, from_time, to_time) returns the timeseries between [from_time, to_time] (UTC)
"""

from redis import Redis as _Redis
from redis import RedisError
from redistimeseries.client import Client as _RTSClient
import logging
import time
import sys


class PCRedis(object):
    """
    The PCRedis class is the wrapper created for use in the PICTURE-C control software. A host, port, and database (db)
    must be specified to the PCRedis.redis client.
    Optionally, with the timeseries keyword, a PCRedis.redistimeseries
    client can also be created. This will use the same host, port, and db. Redistimeseries extends redis' capabilities
    with a module to allow easy time series data storage, instead of creating homemade ways to do that same thing.
    Redistimeseries keys should be created with the PCRedis object. Unlike normal redis keys, they must be created
    explicitly and should be done at the each program's start for clarity and ease.
    """
    def __init__(self, host='localhost', port=6379, db=0, create_ts_keys=tuple()):
        self.redis = _Redis(host, port, db, socket_keepalive=True)
        self.redis_ts = None
        self.create_ts_keys(create_ts_keys)
        self.ps = None  # Redis pubsub object. None until initialized, used for inter-program communication

    def _connect_ts(self):
        """ Establish a redis time series client using the same connection info as for redis """
        args = self.redis.connection_pool.connection_kwargs
        self.redis_ts = _RTSClient(args['host'], args['port'], args['db'],  socket_keepalive=args['socket_keepalive'])

    def create_ts_keys(self, keys):
        """
        Given a list of keys, create them in the redis database.
        :param keys: List of strings to create as redis timeseries keys. If the keys have been created it will be
        logged but no other action will be taken.
        """
        if self.redis_ts is None and keys:
            self._connect_ts()
        for k in keys:
            try:
                self.redis_ts.create(k)
            except RedisError:  # TODO can this be more explicit
                logging.getLogger(__name__).debug(f"'{k}' already exists")

    def store(self, data, timeseries=False):
        """
        Function for storing data in redis. This is a wrapper that allows us to store either type of redis key:value
        pairs (timeseries or 'normal'). Any TS keys must have been previously created
        :param data: Dict or iterable of key value pairs.
        :param timeseries: Bool
        If True: uses redis_ts.add() and uses the automatic UNIX timestamp generation keyword (timestamp='*')
        If False: uses redis.set() and stores the keys normally
        :return: None
        """
        generator = data.items() if isinstance(data, dict) else iter(data)
        if timeseries:
            if self.redis_ts is None:
                self._connect_ts()
            for k, v in generator:
                logging.getLogger(__name__).info(f"Setting key:value - {k}:{v} at {int(time.time())}")
                self.redis_ts.add(key=k, value=v, timestamp='*')
        else:
            for k, v in generator:
                logging.getLogger(__name__).info(f"Setting key:value - {k}:{v}")
                self.redis.set(k, v)

    def publish(self, channel, message):
        """
        Publishes message to channel. Channels need not have been previously created nor must there be a subscriber.

        returns the number of listeners of the channel
        """
        return self.redis.publish(channel, message)

    def read(self, keys, return_dict=True, error_missing=True):
        """
        Function for reading values from corresponding keys in the redis database.
        :param error_missing: riase an error if a key isn't in redis, else silently omit it. Forced true if not
         returning a dict.
        :param keys: List. If the key being searched for exists, will return the value, otherwise returns an empty string
        :param return_dict: Bool
        If True returns a dict with matching key:value pairs
        If False returns a list whose elements correspond to the input keys list. (Not recommended if you have more
        than one key you are looking for the value of)
        :return: Dict. {'key1':'value1', 'key2':'value2', ...}
        """
        vals = [self.redis.get(k) for k in keys]
        missing = [k for k,v in zip(keys, vals) if v is None]
        keys, vals = list(zip(*filter(lambda x: x[1] is not None, zip(keys, vals))))

        if (error_missing or not return_dict) and missing:
            raise KeyError(f'Keys not in redis: {missing}')

        vals = list(map(lambda v: v.decode('utf-8'), vals))
        return vals if not return_dict else dict(zip(keys, vals))

    def _ps_subscribe(self, channels: list, ignore_sub_msg=False):
        """
        Function which will create a redis pubsub object (in self.ps) and subscribe to the keys given. It will also
        raise an error if there is a problem connecting to redis. This will occur either because the redis-server is not
        started or because the host/port was given incorrectly.
        :param channels: List of channels to subscribe to (even if only one channel is being subscribed to)
        :param ignore_sub_msg: Bool
        If True: No message will be sent upon the initial subscription to the channel(s)
        If False: For each channel subscribed to, a message with message['type']='subscribe' will be received.
        :return: None. Will raise an error if the program cannot communicate with redis.
        """
        logging.getLogger(__name__).info(f"Subscribing redis to {channels}")
        try:
            logging.getLogger(__name__).debug(f"Initializing redis pubsub object")
            self.ps = self.redis.pubsub(ignore_subscribe_messages=ignore_sub_msg)
            [self.ps.subscribe(key) for key in channels]
            logging.getLogger(__name__).info(f"Subscribed to: {self.ps.channels}")
        except RedisError as e:
            self.ps = None
            logging.getLogger(__name__).warning(f"Cannot create and subscribe to redis pubsub. Check to make sure redis is running! {e}")
            raise e

    def _ps_unsubscribe(self):
        """
        Unsubscribe from all of the channels that self.ps is currently subscribed to. Sets self.ps to None
        :return: No return. Will raise an error if the program cannot communicate with redis.
        """
        try:
            self.ps.unsubscribe()
            self.ps = None
        except RedisError as e:
            logging.getLogger(__name__).warning(f"Some new error with redis. Check the logs and try restaring! {e}")
            raise e

    def ps_listen(self, channels: list, message_handler, status_key=None, loop_interval=0.001, ignore_sub_msg=False):
        """
        This is the heart of redis pubsub communication between programs. The ps_listen() function is designed to
        incorporate (un)subscribing, message handling, and error handling.
        Tries first to subscribe to the channels passed to the function.
        If able to successfully subscribe, wait for messages to be published and then handle them accordingly. As a
        first pass this means determining message type and logging it properly, then passing it to the message handler.
        The default message handler ( PCRedis.handler() ) simply prints the message. Each agent will overwrite this
        default handler to best suit its own needs.
        :param channels: List of channels to subscribe to
        :param message_handler: Function which properly handles the data in the message and manipulates it accordingly.
        :param status_key: Any status_key required by the program/agent to write to the redis db to record that it is
        working as expected (e.g. "status:device:currentduino:status":"error: could not send message to currentduino")
        :param loop_interval: Float. Time between message queries. This should not be longer than the fastest publishing
        rate in the system
        :param ignore_sub_msg: Bool. See PCRedis._pc_subscribe() for details.
        :return: None. Raises errors in the case of inability to communicate with redis or with a serial port.
        """
        try:
            self._ps_subscribe(channels=channels, ignore_sub_msg=ignore_sub_msg)
        except RedisError as e:
            logging.getLogger(__name__).warning(f"Redis can't subscribe to {channels}. Check to make sure redis is running")
            raise e

        while True:
            try:
                msg = self.ps.get_message()
                if msg:
                    if msg['type'] == 'message':
                        logging.getLogger(__name__).info(f"Redis pubsub client received a message")
                        message_handler(msg)
                    elif msg['type'] == 'subscribe':
                        logging.getLogger(__name__).debug(f"Redis pubsub received subscribe message:\n {msg}")
                    else:
                        logging.getLogger(__name__).info(f"New type of message received! You're on your own now:\n {msg}")
                    if status_key:
                        self.store({status_key: 'okay'})
            except RedisError as e:
                logging.getLogger(__name__).warning(f"Exception in pubsub operation has occurred! Check to make sure "
                                                    f"redis is still running! {e}")
                raise e
            except IOError as e:
                logging.getLogger(__name__).error(f"Error: {e}")
                if status_key:
                    self.store({status_key: f"Error: {e}"})
            time.sleep(loop_interval)

    def listen(self, channels):
        """
        Sets up a subscription for the iterable keys, yielding decoded messages as (k,v) strings.
        Passes up any redis errors that are raised
        """
        log = logging.getLogger(__name__)
        try:
            ps = self.redis.pubsub()
            ps.subscribe(list(channels))
        except RedisError as e:
            log.debug(f"Redis error while subscribing to redis pubsub!! {e}")
            raise e

        for msg in ps.listen():
            log.debug(f"Pubsub received {msg}")
            if msg['type'] == 'subscribe':
                continue
            key = msg['channel'].decode()
            value = msg['data'].decode()

            yield key, value

    def handler(self, message):
        """
        Default pubsub message handler. Prints received message and nothing else.
        Should be overwritten in agent programs.
        :param message: Pubsub message (dict)
        :return: None.
        """
        print(f"Default message handler: {message}")
