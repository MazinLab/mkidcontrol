"""
Author: Noah Swimmer, 15 June 2023

Program to immediately run a "standard" magnet cycle.

Assumes that the heatswitchAgent and lakeshore625Agent are running.

Attempts to close the heat switch and wait for 10 minutes for thermalization

Ramps the current to 9.25 A at 5 mA/s, soaks for ~60 minutes, and deramps the magnet also at 5 mA/s.
"""

import mkidcontrol.mkidredis as redis
import mkidcontrol.agents.xkid.heatswitchAgent as hs
import time
from datetime import datetime, timedelta


if __name__ == "__main__":
    s = datetime.now()
    print(f'close heatswitch at {s.timestamp()}')
    while True:
        t = time.time()
        start = s.timestamp()
        if t >= start:
            print("Closing heat switch")
            hs.close()
            break
        else:
            if int(start-t) % 200 == 0:
                print(f"{(start-t)/60:.2f} minutes until closing heatswitch")
        time.sleep(1)

    s += timedelta(minutes=10)  # Change this line to change when the magnet starts ramping
    print(f'start cycle at {s.timestamp()}')
    while True:
        t = time.time()
        start = s.timestamp()
        if t >= start:
            print('Starting Cooldown!')
            redis.publish('command:device-settings:ls625:desired-current', 9.25, store=False)
            break
        else:
            if int(start-t) % 200 == 0:
                print(f"{(start-t)/60:.2f} minutes until cooldown")
        time.sleep(1)

    s += timedelta(minutes=90)
    print(f'start ramp down at {s.timestamp()}')
    while True:
        t = time.time()
        start = s.timestamp()
        if t >= start:
            hs.open()
            time.sleep(2)
            if hs.is_opened():
                print('open! Ramping down')
                redis.publish('command:device-settings:ls625:desired-current', 0.0, store=False)
            break
        else:
            if int(start-t) % 200 == 0:
                print(f"{(start-t)/60:.2f} minutes until ramp down")
        time.sleep(1)
