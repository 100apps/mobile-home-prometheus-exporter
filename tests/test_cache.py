import sys,threading,time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auto_exporter import AutoExporter
from socket_exporter import Failure

def make():
 cfg={'username':'10000000000','password':'synthetic','devices':{'plug':'id:synthetic'},'cache_ttl':10,'timeout':1}
 e=AutoExporter(cfg);e.key='synthetic';e.attempted=True;e.devices={'plug':'synthetic'};e.names={'plug':'Test'};e.discovered_at=time.monotonic()
 return e

def sample(age=0):return {'values':{'power':12.5},'connected':True,'at':time.time()-age,'mono':time.monotonic()-age}
e=make();e.cache={'plug':sample(40)};e.error='http_error';e.fetch_up={'plug':0}
m=e.metrics();assert 'parameter="power"' in m and 'andlink_device_cache_stale{device="plug"} 1' in m
assert 'andlink_exporter_up 0' in m
assert not e.busy and e.worker is None
# No stale raw value after the hard 60-second deadline.
e.cache={'plug':sample(61)};assert 'parameter="power"' not in e.metrics()
# Failed refresh retains the last successful sample; never extends its timestamp.
e=make();old=sample(10);e.cache={'plug':old}
def fail(*_):raise Failure('http_error')
e.cloud.detail=fail;e.refresh();assert e.cache['plug'] is old and e.fetch_up['plug']==0
# Background polling proceeds without any HTTP scrape; slow cloud call cannot block metrics.
e=make();e.cache={'plug':sample()};started=threading.Event();release=threading.Event();calls=[]
def slow(*_):calls.append(1);started.set();release.wait(5);return sample()
e.cloud.detail=slow;e.start();assert started.wait(1)
t=time.perf_counter()
for _ in range(100):e.metrics()
elapsed=(time.perf_counter()-t)*1000
assert elapsed<200 and len(calls)==1
release.set();e.shutdown.set();e.worker.join(3);assert not e.worker.is_alive()
# Rate limits keep cache briefly but pause polling for the vendor-required interval.
e=make();e.cache={'plug':sample()}
def limited(*_):raise Failure('rate_limited',900)
e.cloud.detail=limited;e.refresh();assert e.cache and e.due-time.monotonic()>890
print('PASS: 60s expiry, failure retention, stale labels, autonomous polling, no overlap, vendor backoff.')
print('100 in-memory metric responses during blocked cloud I/O: %.2f ms total' % elapsed)
