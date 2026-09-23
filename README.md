# ESP32-S3 board workspace

Development workspace for Waveshare ESP32-S3 boards, pinned to ESP-IDF v5.5.3.

## Boards

| Board | SoC | Flash / PSRAM | Display | Onboard |
|---|---|---|---|---|
| ESP32-S3-RLCD-4.2 (x2) | ESP32-S3-WROOM-1-N16R8 | 16MB / 8MB | 4.2" reflective mono, 400x300, ST7305, SPI | ES8311 + ES7210, dual mic, PCF85063 RTC, SHTC3, TF, 18650 |
| ESP32-S3-Touch-LCD-3.49 (x2) | ESP32-S3R8 | 16MB / 8MB | 3.49" IPS touch, 172x640, AXS15231B, QSPI + I2C | TCA9554 expander, ES8311 + ES7210, dual mic, QMI8658 IMU, PCF85063 RTC, TF |

### 3.49 revision check

V1 and V2 are pin-incompatible and the vendor examples are NOT interchangeable.

- V2 identification: silkscreen `Rev1.1` or QC label `V2` (shipped since 2026-06-08)
- V2 examples: `vendor/ESP32-S3-Touch-LCD-3.49-V2`
- V1 examples: https://github.com/waveshareteam/ESP32-S3-Touch-LCD-3.49

## Pinned versions

- ESP-IDF **v5.5.3**, installed via EIM into `~/.espressif`, target `esp32s3` only
- 3.49 V2 examples default to IDF v5.5.3; RLCD-4.2 requires >= v5.5.0
- Arduino path (optional): esp32 core 3.3.0 + LVGL 8.3.11/9.3.0 + SensorLib 0.3.1

## Setup on a new workstation

```sh
yay -S eim-cli                 # or install EIM from Espressif's official pacman repo
git clone --recurse-submodules --shallow-submodules <repo-url> esp32
cd esp32
./scripts/setup.sh             # installs IDF v5.5.3 if missing, syncs submodules
. ./scripts/env.sh             # activate ESP-IDF in this shell
```

Serial access requires membership in the `uucp` group:

```sh
sudo usermod -aG uucp "$USER"  # then log out and back in
```

## Layout

```
projects/    ESP-IDF apps, one directory each
components/  shared components (add via EXTRA_COMPONENT_DIRS)
vendor/      pinned upstream repos (git submodules)
scripts/     setup.sh, env.sh, eim-config.toml
```

## Projects

- `projects/349-hello` — 640x172 landscape bring-up/demo for the
  ESP32-S3-Touch-LCD-3.49 V2. See its README for the display pipeline and the
  panel quirks (40MHz QSPI limit, no partial writes, no hardware rotation).

## Quickstart

```sh
. scripts/env.sh
idf.py create-project -p projects/my-app my-app
cd projects/my-app
idf.py set-target esp32s3
idf.py build flash monitor
```

## References

- ESP32-S3-RLCD-4.2: https://docs.waveshare.com/ESP32-S3-RLCD-4.2
- ESP32-S3-Touch-LCD-3.49: https://docs.waveshare.com/ESP32-S3-Touch-LCD-3.49
- ESP-IDF v5.5.3: https://docs.espressif.com/projects/esp-idf/en/v5.5.3/
- EIM: https://docs.espressif.com/projects/idf-im-ui/en/latest/
