#!/bin/sh
set -eu
ulimit -c 0
test "$(uname -m)" = aarch64
source=/input/original/acceptance/source
trap 'status=$?; trap - EXIT; if test -d /task/evidence; then cp -a /task/evidence /output/runtime; fi; if test -d /task/integration; then /usr/local/bin/python -I -B "$source/tools/acceptance/prepare_evidence.py" /task/integration /output/integration || status=97; fi; exit "$status"' EXIT
apk add --no-cache build-base linux-headers zlib-dev bzip2-dev xz-dev libffi-dev openssl-dev readline-dev sqlite-dev patchelf
apk info -v musl gcc libstdc++ patchelf > /output/packages.txt
export PYTHON_JIT=1 PYTHONDONTWRITEBYTECODE=1
/usr/local/bin/python -I -B /input/build_pair.py
export LD_LIBRARY_PATH=/task/cpython-3.15.2
mkdir /task/integration
exec > /task/integration/driver.stdout.log 2> /task/integration/driver.stderr.log
/task/cpython-3.15.2/python -I -B -m venv /task/integration-env
python=/task/integration-env/bin/python
set -- /input/original/acceptance/distributions/*.whl
test "$#" = 1
wheel=$1
"$python" -m pip install setuptools==84.2.1 auditwheel==6.8.2 "${wheel}[test]"
cp -R "$source/tools/acceptance/native_client" /task/integration/native-client
cp "$source/src/greenlet/greenlet.h" /task/integration/native-client/greenlet.h
cd /task/integration/native-client
"$python" -I -B setup.py build_ext --inplace
"$python" -I -B "$source/tools/acceptance/qualify_release_wheel.py" "$wheel" "$source" /task/integration/qualified /task/integration/native-client
"$python" -I -B "$source/tools/acceptance/check_cross_thread_gc.py" /task/integration/cross-thread-GC
"$python" -I -B "$source/tools/acceptance/check_failed_initialization.py" /task/integration/failed-init /task/integration/native-client
"$python" -I -B "$source/tools/acceptance/check_si_teardown.py" /task/integration/teardown
"$python" -I -B -c 'import json,os,platform,sys,sysconfig; from pathlib import Path; assert platform.machine()=="aarch64" and sys.version_info[:3]==(3,15,2) and sysconfig.get_config_var("Py_GIL_DISABLED")==1 and not sys._is_gil_enabled() and sys.flags.gil is None and "PYTHON_GIL" not in os.environ; Path("/task/integration/RESULT.json").write_text(json.dumps({"status":"PASS_NATIVE_MUSL_ARM64_ON_CORRECTED_CPYTHON_SOURCE_RUNTIME", "original_greenlet_head":"eb651bdeb4cfb8dd66467be3214598ee38ea950a", "runtime":sys.version,"architecture":platform.machine(),"GIL":False,"gil_flag":None},indent=2))'
