"""
Author: Noah Swimmer 29 June 2020

A wrapper class to conveniently use redis-py and redistimeseries with PICTURE-C. This includes but is not limited to
inter-program communication (using pubsub), information storage (of device settings), and data storage (thermometry,
current, etc.).
"""

from redis import Redis as _Redis
from redis import RedisError, ConnectionError, TimeoutError, AuthenticationError, BusyLoadingError, \
    InvalidResponse, ResponseError, DataError, PubSubError, WatchError, \
    ReadOnlyError, ChildDeadlockedError, AuthenticationWrongNumberOfArgsError
from redistimeseries.client import Client as _RTSClient
import logging
import datetime
# from .config import REDIS_DB

REDIS_DB = 0


class MKIDRedis(object):
    """
    The MKIDRedis class is the wrapper created for use in the PICTURE-C control software. A host, port, and database (db)
    must be specified to the MKIDRedis.redis client.
    Optionally, with the timeseries keyword, a MKIDRedis.redistimeseries
    client can also be created. This will use the same host, port, and db. Redistimeseries extends redis' capabilities
    with a module to allow easy time series data storage, instead of creating homemade ways to do that same thing.
    Redistimeseries keys should be created with the MKIDRedis object. Unlike normal redis keys, they must be created
    explicitly and should be done at the each program's start for clarity and ease.
    """
    def __init__(self, host='localhost', port=6379, db=REDIS_DB, ts_keys=tuple()):
        self.redis = _Redis(host, port, db, socket_keepalive=True)
        self.redis_ts = None
        self._connect_ts()

        self.ts_keys = ts_keys
        self.create_ts_keys(ts_keys)

        self.ps = None  # Redis pubsub object. None until initialized, used for inter-program communication

    def _connect_ts(self, force=False):
        """ Establish a redis time series client using the same connection info as for redis """
        if self.redis_ts is not None and not force:
            return
        args = self.redis.connection_pool.connection_kwargs
        self.redis_ts = _RTSClient(args['host'], args['port'], args['db'],  socket_keepalive=args['socket_keepalive'])

    def create_ts_keys(self, keys):
        """
        Given a list of keys, create them in the redis database.
        :param keys: list of strings to create as redis timeseries keys. If the keys have been created it will be
        logged but no other action will be taken.

        keys may a single string for a single key or a dictionary of key:retention_time_ms pairs
        """

        if self.redis_ts is None and keys:
            self._connect_ts()

        for k in keys:
            try:
                self.redis_ts.create(k)
            except ResponseError:
                logging.getLogger(__name__).debug(f"Redistimeseries key '{k}' already exists.")

    def store(self, data, timeseries=False):
        """
        Function for storing data in redis. This is a wrapper that allows us to store either type of redis key:value
        pairs (timeseries or 'normal'). Any TS keys must have been previously created.
        If not storing timeseries keys, the value is published to the channel with the name of the key.
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
                logging.getLogger(__name__).info(f"Setting ts {k} to {v}")
                self.redis_ts.add(key=k, value=v, timestamp='*')
        else:
            for k, v in generator:
                logging.getLogger(__name__).info(f"Setting {k} to {v}")
                self.redis.set(k, v)
                self.publish(k, v, store=False)

    def publish(self, channel, message, store=True):
        """
        Publishes message to channel. Channels need not have been previously created nor must there be a subscriber.
        returns the number of listeners of the channel
        """
        if store:
            self.store({channel: message})
        return self.redis.publish(channel, message)

    def read(self, keys: (list, tuple, str), error_missing=True):
        """
        Function for reading values from corresponding keys in the redis database.
        :param error_missing: raise an error if a key isn't in redis, else silently omit it and return None
        :param keys: List|str|tuple, the redis keys to search
        :param return_dict: Bool
        :return: Dict | Str | Tuple | None
        If multiple keys are queried, a dict is returned where dict = {'k1':'v1', 'k2':'v2', ... }
        If a single timeseries key is queried, a tuple is returned where tuple = (UNIX timestamp in ms, val, timestamp in HH:MM:SS)
        If a single non-timeseries key is queried, str = 'val'
        If the key does not exist and error_missing=False, returns None
        """
        if isinstance(keys, str):
            keys = [keys]

        for k in range(len(keys)):
            try:
                keys[k] = keys[k].decode('utf-8')
            except AttributeError:
                pass

        if len(keys) > 1:
            vals = []

            for k in keys:
                if k in self.ts_keys:
                    try:
                        v = self.redis_ts.get(k)
                        vals.append(v + (datetime.datetime.fromtimestamp(v[0] / 1000).strftime("%H:%M:%S"),))
                    except (ResponseError, TypeError):
                        vals.append(None)
                else:
                    try:
                        vals.append(self.redis.get(k).decode('utf-8'))
                    except AttributeError:
                        vals.append(None)

            missing = [k for k, v in zip(keys, vals) if v is None]

            if error_missing and missing:
                raise KeyError(f'Keys not in redis: {missing}')

            return dict(zip(keys, vals))
        else:
            if keys[0] in self.ts_keys:
                try:
                    v = self.redis_ts.get(keys[0])
                    val = (v + (datetime.datetime.fromtimestamp(v[0] / 1000).strftime("%H:%M:%S"), ))
                except ResponseError:
                    if error_missing:
                        raise KeyError(f"Key not in redis: {keys[0]}")
                    else:
                        val = None
            else:
                try:
                    val = self.redis.get(keys[0]).decode('utf-8')
                except AttributeError:
                    if error_missing:
                        raise KeyError(f"Key not in redis: {keys[0]}")
                    else:
                        val = None

            return val

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

    def listen(self, channels:(list, tuple, str)):
        """
        Sets up a subscription for the iterable keys, yielding decoded messages as (k,v) strings.
        Passes up any redis errors that are raised
        """
        log = logging.getLogger(__name__)
        if isinstance(channels, str):
            channels = [channels]
        try:
            ps = self.redis.pubsub()
            ps.subscribe(channels)
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

    def range(self, key: str, start=None, end=None):
        if isinstance(start, (int, float)):
            start = int(start)
        elif isinstance(start, str):
            if start == "-":
                pass
            else:
                int(start)
        elif start is None:
            start = 0
        else:
            start = int(start.timestamp() * 1000)
        if isinstance(end, (int, float)):
            end = int(end)
        elif isinstance(end, str):
            if end == "+":
                pass
            else:
                int(end)
        elif end is None:
            end = -1
        else:
            end = int(end.timestamp() * 1000)

        rang = self.redis_ts.range(key, start, end)
        if len(rang):
            return rang
        else:
            return [(None, None)]

mkidredis = None
store = None
read = None
listen = None
publish = None
mkr_range = None  # This breaks the naming mold since range is already a python special function
redis_ts = None

def setup_redis(host='localhost', port=6379, db=REDIS_DB, ts_keys=tuple()):
    global mkidredis, store, read, listen, publish, mkr_range, redis_ts, redis_keys
    mkidredis = MKIDRedis(host=host, port=port, db=db, ts_keys=ts_keys)
    store = mkidredis.store
    read = mkidredis.read
    listen = mkidredis.listen
    publish = mkidredis.publish
    mkr_range = mkidredis.range
    redis_ts = mkidredis.redis_ts
    redis_keys = mkidredis.redis.keys
