import logging
import redis
import time

if __name__ == "__main__":
    log = logging.getLogger(__name__)
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(message)s')

    r = redis.Redis(host='127.0.0.1', port=6379, db=0, socket_timeout=None, socket_keepalive=True, health_check_interval=25)
    ps = None

    t1 = time.time()
    while True:
        try:
            if ps is None:
                ps = r.pubsub()
                log.info(f"Subcribing to test-channel")
                ps.subscribe("test-channel")

            if ((time.time() - t1) > 5) and ps:
                log.debug("---- checking health ----")
                ps.check_health()
                ps.ping()
                t1 = time.time()

            for msg in ps.listen():
                log.info(f"received{msg}")
            # msg = ps.get_message()
            # if msg:
            #     log.info(f"Pubsub received: {msg}")
            # else:
            #     pass
        except redis.exceptions.ConnectionError as e:
            log.warning(f"Error in redis connection: {e}")
            ps = None

        time.sleep(0.01)

    # while True:
    #     try:
    #         if redis_con is None:
    #             redis_con = redis.Redis(host='127.0.0.1', port=6379, db=0, socket_timeout=None, socket_keepalive=True)
    #
    #         if ps is None:
    #             ps = redis_con.pubsub()
    #
    #         log.debug(f"{time.time()}")
    #         for c in channels:
    #             log.info(f"Subcribing to channel {c}")
    #             ps.subscribe(c)
    #         log.debug(f"{time.time()}")
    #
    #     except redis.ConnectionError as e:
    #         log.warning(f"Redis connection error during subscription! {e}")
    #         redis_con.close()
    #         redis_con = None
    #     except redis.PubSubError as e:
    #         log.warning(f"Redis error in subscribing or reconnecting! {e}")
    #         ps = None
    #
    #     if redis_con:
    #         try:
    #             for msg in ps.listen():
    #                 log.info(f"Received: {msg}")
    #         except redis.ConnectionError as e:
    #             log.warning(f"Redis connection error during message reception for {c}! {e}")
    #             redis_con = None
    #             ps.reset()
    #             ps = None
    #         except redis.PubSubError as e:
    #             log.warning(f"Redis error in subscribing or reconnecting after subscription error! {e}")
    #             ps = None

    # channels = [f'c{i}' for i in range(5)]
    # redis_con = None
    # subs = {c: None for c in channels}
    # subbed = {c: False for c in channels}
    # while True:
    #     try:
    #         if redis_con is None:
    #             redis_con = redis.Redis(host='127.0.0.1', port=6379, db=0, socket_timeout=None, socket_keepalive=True)
    #             # redis_con = redis.Redis(host='127.0.0.1', port=6379, db=0, socket_timeout=None, socket_keepalive=True, socket_keepalive_options={})
    #
    #         for c in channels:
    #             if subs[c] is None or (subbed[c] == False):
    #                 log.debug(f"{time.time()}")
    #                 log.info(f"Subscribing to channel: {c}")
    #                 subs[c] = redis_con.pubsub()
    #                 subs[c].subscribe(c)
    #                 subbed[c] = True
    #                 log.debug(f"{time.time()}")
    #     except redis.ConnectionError as e:
    #         log.warning(f"Redis connection error during subscription! {e}")
    #         redis_con.close()
    #         redis_con = None
    #     except redis.PubSubError as e:
    #         log.warning(f"Redis error in subscribing or reconnecting! {e}")
    #
    #     for c in channels:
    #         if subbed[c]:
    #             subs[c].check_health()
    #
    #     if redis_con:
    #         for c in channels:
    #             try:
    #                 msg = subs[c].get_message()
    #                 if msg:
    #                     log.info(f"Message received: {msg}")
    #             except redis.ConnectionError as e:
    #                 log.warning(f"Redis connection error during message reception for {c}! {e}")
    #                 subs[c].unsubscribe()
    #                 subbed[c] = False
    #                 # subs[c].subscribe(subs[c].channels)
    #             except redis.PubSubError as e:
    #                 log.warning(f"Redis error in subscribing or reconnecting after subscription error! {e}")
    #
    #     time.sleep(0.001)
