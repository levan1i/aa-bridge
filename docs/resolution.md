# Video resolution

The mode is chosen from the car's `Open` request (`aa_session.compute_mode`):

| Car view | AA size offered | Notes |
|---|---|---|
| width ≤ 800 | 800x480 | |
| ≤ 1280 | 1280x720 | |
| larger | 1920x1080 | capped; taller/wider views are cropped to the frame |
| height > width | 720x1280 / 1080x1920 / 1440x2560 (portrait) | **experimental**: depends on the phone accepting portrait |

The car's w×h area is the top-left part of the frame. The rest is excluded with `margin_width/height` and the
`AdditionalVideoConfig.display_insets` (`VideoConfig` field 11: top, bottom, left, right), so the phone draws its UI only
where the car shows it. For the IM L6 (`1920x720`) this gives a 1920x1080 frame with a 360-row bottom inset, exactly what
the car app displays. The same numbers are used for the touch screen config and for mapping car touches
(x/10000·ui_width, y/10000·ui_height). The video message header carries the frame size, not the UI size.

If the car's `Open` arrives after the phone session is running and needs another mode, the session is dropped so the
phone reconnects with the right one. `AA_RES` forces a size. `BoxSettings` also contains `androidAutoSizeW/H`; it is
logged but not used yet.
