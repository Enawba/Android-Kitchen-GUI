# Changelog

## 1.0.1 (2026-11-28)

- **Fixed:** the *Build flash script*, *Extract selected* and *Cancel* buttons were cut
  off on lower-resolution screens or when the window wasn't maximised.
  - The action bar is now docked at the bottom, so the partition list and the log shrink
    instead.
  - On narrow windows the progress bar shrinks before the buttons do.
- **Fixed:** the log panel no longer collapses to nothing on short windows; it keeps at
  least about 110 px, and you can still drag the divider.
- **Fixed:** normal log lines were hard to read (light grey on white) on some Windows
  colour schemes. The log now uses dark text.
- The window now opens no larger than the screen, and its minimum size is smaller
  (760×480).

## 1.0.0 (2026-11-26)

- First release: extract APKs and configs from stock firmware (zip, tar/md5, payload.bin,
  super / sparse / sdat images, ext4 and EROFS).
- Flash-script generator for going back to stock (OnePlus-safe order).
