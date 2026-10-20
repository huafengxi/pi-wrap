"""Boot identity must survive wall-clock correction and reject bad native data."""
import ctypes,errno,os,subprocess,sys,unittest
from types import SimpleNamespace
from unittest.mock import patch
import local_process as O
class BootTests(unittest.TestCase):
 def setUp(self):O.boot_id.cache_clear()
 def tearDown(self):O.boot_id.cache_clear()
 def fake(self,data=b'E634D790-2D40-4476-AB2F-D84C3324A6AD\0',size=37,rc=0):
  def call(name,value,length,new,newlen):
   self.assertEqual(name,b'kern.bootsessionuuid');self.assertIsNone(new);self.assertEqual(newlen,0)
   ctypes.memmove(value,data,min(len(data),37));ctypes.cast(length,ctypes.POINTER(ctypes.c_size_t))[0]=size;ctypes.set_errno(errno.EACCES);return rc
  return SimpleNamespace(sysctlbyname=call)
 def test_native_uuid_and_cache_refresh(self):
  with patch.object(O.sys,'platform','darwin'),patch.object(O.ctypes,'CDLL',return_value=self.fake()):
   a=O.boot_id();O.boot_id.cache_clear();self.assertEqual(O.boot_id(),a)
   self.assertEqual(a,'darwin:e634d790-2d40-4476-ab2f-d84c3324a6ad')
 def test_denial_is_error_without_time_fallback(self):
  with patch.object(O.sys,'platform','darwin'),patch.object(O.ctypes,'CDLL',return_value=self.fake(rc=-1)):
   with self.assertRaises(OSError):O.boot_id()
 def test_malformed_and_truncated_are_errors(self):
  for data,size in ((b'x'*36+b'\0',37),(b'0'*36+b'\0',37),(b'x'*37,37),(b'valid',5)):
   O.boot_id.cache_clear()
   with patch.object(O.sys,'platform','darwin'),patch.object(O.ctypes,'CDLL',return_value=self.fake(data,size)):
    with self.assertRaises(RuntimeError):O.boot_id()
 def test_actual_fresh_process_same_boot(self):
  expression='import local_process;print(local_process.boot_id())'
  actual=subprocess.check_output([sys.executable,'-B','-c',expression],text=True).strip()
  self.assertEqual(actual,O.boot_id())
  if sys.platform=='darwin':self.assertEqual(O.boot_id(),'darwin:'+subprocess.check_output(['/usr/sbin/sysctl','-n','kern.bootsessionuuid'],text=True).strip().lower())
if __name__=='__main__':unittest.main(verbosity=2)
