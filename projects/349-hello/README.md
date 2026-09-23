# 349-hello — ESP32-S3-Touch-LCD-3.49 V2

Landscape (640x172) bouncing-ball demo used to bring up the board and tune the
display pipeline. Runs at ~45fps with low CPU and no artifacts.

## Build and flash

```sh
. ../../scripts/env.sh
idf.py build
idf.py -p /dev/ttyACM0 flash monitor
```

## Hardware and configuration

- Panel: AXS15231B, 172x640 native (portrait), QSPI on SPI3, I2C touch on a
  separate bus
- TCA9554 I2C expander gates the backlight enable and LCD reset
- ESP-IDF v5.5.3, LVGL v9 (managed component), CPU at 240MHz, 80MHz octal PSRAM
- LVGL in DIRECT render mode with a single full-screen RGB565 buffer in PSRAM
  (220KB)
- QSPI clock: 40MHz (see finding 2)

## Display pipeline

1. LVGL renders the UI in landscape (640x172) into the PSRAM framebuffer.
2. The flush callback transposes each dirty rectangle into a persistent
   **shadow framebuffer** in the panel's native portrait orientation (172x640,
   big-endian RGB565).
3. The whole shadow is sent every frame in 10 full-width row chunks (64 rows
   each), staged through two internal DMA buffers using the async memcpy (GDMA)
   driver.

Why this shape:

- The panel cannot rotate in hardware over QSPI (finding 1), so software
  rotation is mandatory.
- The panel only accepts complete frames (finding 3), so the 220KB transfer is
  unavoidable; the shadow instead makes the *transpose* proportional to the
  dirty area, and the GDMA staging keeps the CPU out of the copy.

## Findings

1. **Hardware rotation is not usable.** The AXS15231B datasheet limits its 90
   degree rotation function to <=360 columns; our landscape needs 640. Driving
   MADCTL swap_xy/mirror over QSPI produced garbage. Waveshare's own config
   notes "software rotation" (软件实现旋转).
2. **QSPI tops out at 40MHz on this panel.** At 80MHz the panel silently fails
   to latch the tail of each row, leaving a full-width noise strip at the native
   column edge. The vendor's factory firmware runs 40MHz; verified by flashing
   the factory binary.
3. **No partial-rectangle writes.** Sending CASET + RASET + RAMWR for a bounded
   rectangle is ignored by the panel (the screen freezes after the first full
   frame). It requires continuous full-width row writes; the Espressif driver
   deliberately omits RASET in QSPI mode for this reason.
4. **LVGL has no Xtensa-optimized rotate.** `LV_DRAW_SW_ROTATE90_RGB565` is
   undefined, so `lv_draw_sw_rotate` falls back to a scalar transpose with
   1280-byte-strided reads: about 64ms per 110K-pixel frame (~70% of the frame
   time). The shadow framebuffer removes it from the hot path.
5. **A CPU memcpy of the 220KB shadow costs as much as the transpose did**
   (PSRAM-bound). `esp_async_memcpy` (GDMA) offloads it and overlaps the SPI
   transfer; dragging CPU dropped from ~50% to low single digits.
6. **Touch mapping.** The controller reports in 640x172 landscape space; LVGL
   applies the display rotation itself, so the indev callback must return native
   (172x640) coordinates: `x = raw_y`, `y = 639 - raw_x`.
7. **LVGL's perf-monitor CPU% is wrong on ESP-IDF by default.** Its idle reader
   depends on FreeRTOS trace hooks that must be wired manually, and it matches
   the task name "IDLE" while ESP-IDF names the idle tasks IDLE0/IDLE1, so it
   always reported 100%. Fix: enable
   `CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS`, implement an idle reader with
   `ulTaskGetIdleRunTimeCounterForCore(0)`, and point `LV_SYSMON_GET_IDLE` at it
   with a compile definition on the lvgl component (see `main/CMakeLists.txt`).

## Performance history

Continuous animation, 640x172 landscape, measured on-device:

| Stage | Frame period | Notes |
|---|---|---|
| Vendor example (portrait) | ~90ms | LVGL scalar rotate fallback dominates |
| Fused transpose + 240MHz + double-buffered chunks | ~38ms | |
| Flush coalescing + 8ms refresh/input period | ~20ms | input sampling was the ceiling |
| Touch read on its own task (core 1) | ~15.5ms | |
| Shadow framebuffer + dirty-rect transpose | ~16ms | CPU ~40% |
| + GDMA async staging copies | ~16ms | CPU low single digits |
| QSPI 80 -> 40MHz (correctness fix) | ~22ms | ~45fps, artifact-free |

## Notes

- `sdkconfig` is generated; `sdkconfig.defaults` is the source of truth.
- The perf overlay's idle FPS reading is meaningless: with the perf monitor
  enabled LVGL keeps the refresh timer running and counts timer ticks, not
  rendered frames.
