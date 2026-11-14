/* dcmgr capture-delay calibration for the Dreamcast (KallistiOS).
 *
 * Alternates the whole screen between black and white. Right after each switch to white it prints
 * "CAL flash <n> fill_us=<us>" on the console, so the console line and the change on screen happen
 * together. tools/calibrate_delay.py then finds each flash in the session's recording and compares
 * when it shows up there with when its line was printed: the median difference is capture_delay_ms.
 *
 * Frame pacing blocks on a vblank genwait object (JM-17) with the scheduler at 333 Hz (JM-14); the
 * fill is timed with the perf counter (JM-16), never the ms/us timers (JM-15).
 */
#include <kos.h>
#include <arch/irq.h>
#include <dc/perfctr.h>
#include <dc/vblank.h>
#include <dc/video.h>
#include <kos/genwait.h>
#include <kos/thread.h>
#include <stdio.h>

#define FLASHES 12
#define WHITE_FRAMES 30   /* 0.5 s white */
#define BLACK_FRAMES 60   /* 1 s black between flashes */

static volatile uint32_t vbl_count;

static void on_vblank(uint32_t code, void *data) {   /* interrupt context */
    (void)code;
    (void)data;
    vbl_count++;
    genwait_wake_all((void *)&vbl_count);
}

/* Block until n more vblanks have happened. */
static void wait_vblanks(uint32_t n) {
    irq_mask_t old = irq_disable();       /* check-then-sleep without missing a wake */
    uint32_t target = vbl_count + n;
    while ((int32_t)(vbl_count - target) < 0)
        genwait_wait((void *)&vbl_count, "calib", 0);  /* this KOS: (obj, mesg, timeout); 0 = none */
    irq_restore(old);
}

int main(int argc, char **argv) {
    (void)argc;
    (void)argv;
    thd_set_hz(333);
    vid_set_mode(DM_640x480, PM_RGB565);
    int vbl = vblank_handler_add(on_vblank, NULL);
    vid_clear(0, 0, 0);
    printf("CAL start flashes=%d white_frames=%d black_frames=%d\n", FLASHES, WHITE_FRAMES, BLACK_FRAMES);
    fflush(stdout);
    wait_vblanks(120);  /* let the capture settle on black */

    for (int i = 1; i <= FLASHES; i++) {
        wait_vblanks(1);  /* start just after a vblank: the fill runs ahead of the beam */
        uint64_t t0 = perf_cntr_timer_ns();
        vid_clear(255, 255, 255);
        uint32_t fill_us = (uint32_t)((perf_cntr_timer_ns() - t0) / 1000);
        /* The screen went white at about (this line's time - fill_us); the analysis corrects for it. */
        printf("CAL flash %d fill_us=%u\n", i, (unsigned)fill_us);
        fflush(stdout);
        wait_vblanks(WHITE_FRAMES);
        vid_clear(0, 0, 0);
        wait_vblanks(BLACK_FRAMES);
    }

    printf("CAL done\n");
    fflush(stdout);
    vblank_handler_remove(vbl);
    return 0;
}
