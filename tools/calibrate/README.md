# Capture-delay calibration

`capture_delay_ms` lines console lines up with the session recording: how far the captured picture lags
the DC (capture device, ffmpeg, the server). This measures it on your rig.

1. Build the test program with a KallistiOS environment loaded:

       source $KOS_BASE/environ.sh && make

2. Run it on the DC through dcm, as project `calibration` (it takes about 30 s: 12 white flashes, each
   announced on the console as it happens):

       dcm load calib.elf -p calibration      # or: dcm run -p calibration -- kos-tool -t {ip} -x calib.elf

3. Measure, from any machine with Python 3 and ffmpeg:

       python3 analyze.py --url http://<dcmgr-host>

   It prints how long after the DC each flash showed up in the recording, and a recommended value: the
   smallest `capture_delay_ms` at which every flash line's frame (in the session page and `frame.jpg`) already
   shows the flash. Put it in `dcmgr.json` (`"capture_delay_ms": <value>`) and restart dcmgr. Larger values
   still show the change but land on later frames; smaller ones show the frame before it.

The test program needs a host tool matching the DC's loader; with dc-load-ip 3.x that is
[kos-tool](https://github.com/andressbarajas/kos-tool) (`dcm run -- kos-tool -t {ip} -x calib.elf`).
Example: the DC's VGA output through a VGA-to-HDMI adapter into a USB capture stick (MS2109), 30 fps: the
picture lagged the DC by 65-103 ms and the recommended value was 60 ms.

## Licences

`calib.elf` is built with KallistiOS and newlib and includes their code. KallistiOS is under the KOS License
(BSD-style, [LICENSE.KOS](LICENSE.KOS)); newlib's notices are in its source distribution
(`COPYING.NEWLIB`). `calib.c`, `analyze.py` and `scrub_banner.py` are under dcmgr's MIT licence. The Makefile
strips the binary and removes the build machine's user, host name and build date from KOS's banner
(`scrub_banner.py`) before it is shared.
