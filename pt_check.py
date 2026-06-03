"""
Called as: python pt_check.py HH MM
Exits 0 if current PT time >= HH:MM, exits 1 otherwise.
"""
import sys, datetime, time, os

os.environ['TZ'] = 'America/Los_Angeles'
time.tzset()

h, m = int(sys.argv[1]), int(sys.argv[2])
now = datetime.datetime.now()
target = now.replace(hour=h, minute=m, second=0, microsecond=0)
print(f"[TIME] Now: {now.strftime('%H:%M PT')}  Target: {h}:{str(m).zfill(2)} PT  Past: {now >= target}")
sys.exit(0 if now >= target else 1)
