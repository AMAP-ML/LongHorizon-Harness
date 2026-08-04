# Qwen norm1000 coordinate mode

This document explains why our Qwen-class WeaveBench runs use `norm1000`
coordinates by default, while non-Qwen models keep the official WeaveBench
pixel-coordinate default.

## Background

WeaveBench GUI tasks operate a real Ubuntu desktop. The desktop is rendered at a
fixed pixel resolution, and the official computer tool expects raw screen pixel
coordinates. For example, on a `1920x1080` desktop, a click near the center is
roughly:

```text
x=960, y=540
```

This is a natural interface for models and tools that reliably reason in the
actual screen pixel space. It is also the behavior preserved for non-Qwen models
in this repository.

In our Qwen-class runs, however, we observed a recurring mismatch: the model
often reasoned from the screenshot as if the visible image were a normalized
canvas rather than the raw desktop resolution. The resulting actions were
spatially plausible in the screenshot, but numerically wrong for the VM display.
For example, a model may point to the visual center using coordinates around
`x=500, y=500`, which is sensible in a 0-1000 visual frame but lands far from the
intended target if interpreted as raw pixels on a 1920-wide desktop.

## What norm1000 means

`norm1000` defines GUI coordinates in a model-facing 0-1000 coordinate frame:

```text
x=0      left edge of the screenshot
x=1000   right edge of the screenshot
y=0      top edge of the screenshot
y=1000   bottom edge of the screenshot
```

The tool then converts those normalized coordinates to actual VM pixels at
execution time:

```text
pixel_x = round(norm_x / 1000 * screen_width)
pixel_y = round(norm_y / 1000 * screen_height)
```

So on a `1920x1080` desktop:

```text
norm1000 x=500, y=500  ->  pixel x=960, y=540
```

The VM still receives ordinary `pyautogui` pixel actions. `norm1000` only changes
the coordinate contract between the model and the GUI tool.

## Why this helps Qwen

Qwen-class vision behavior in our small ablations was more stable when the tool
contract matched a normalized screenshot frame. This reduces a specific failure
mode: the model chooses visually correct locations but emits coordinates in a
different scale from the VM.

The change is therefore not a new task capability. It does not reveal hidden
state, read files, inspect the answer, or bypass the GUI. It only converts a
coordinate frame before sending the same mouse action to the desktop.

## Why non-Qwen models keep pixel mode

Official WeaveBench uses pixel coordinates. Many existing agents and prompts are
already tuned to that convention. To avoid changing the behavior of unrelated
models, the default is model-dependent:

```text
Qwen-class models      -> norm1000
non-Qwen models        -> pixel
```

The shared resolver is:

```text
weavebench/agents/qwen_compat.py
```

It is used consistently by Claude Code, Codex, OpenClaw, and CUA-Harness
backends so that the same model receives the same coordinate contract across
harnesses.

## How to override it

The environment variable is:

```bash
export WEAVEBENCH_COMPUTER_COORD_MODE=auto
```

Valid values:

```text
auto      Qwen -> norm1000; non-Qwen -> pixel
norm1000  always use normalized 0-1000 coordinates
pixel     always use raw screen pixel coordinates
```

For exact reproduction of our Qwen 3.7-Plus runs, leave the default `auto` mode
or set:

```bash
export WEAVEBENCH_COMPUTER_COORD_MODE=norm1000
```

For strict official-pixel behavior, set:

```bash
export WEAVEBENCH_COMPUTER_COORD_MODE=pixel
```

Individual GUI actions may also override the process default with:

```json
{"type": "click", "x": 960, "y": 540, "coordinate_space": "pixel"}
```

or:

```json
{"type": "click", "x": 500, "y": 500, "coordinate_space": "norm1000"}
```

## Interaction with image proxy

`norm1000` and image URL proxy solve different problems:

```text
norm1000      coordinate-scale mismatch between model output and VM pixels
image proxy   large screenshot payloads rejected by some API gateways
```

They are enabled together by default for Qwen-class models because both were
needed for stable Qwen GUI runs in our setting, but they are controlled by
separate environment variables:

```bash
export WEAVEBENCH_COMPUTER_COORD_MODE=auto
export WEAVEBENCH_IMAGE_PROXY=1
```

If your provider accepts large base64 images directly, you may set
`WEAVEBENCH_IMAGE_PROXY=0` while still keeping `norm1000`.

## Fairness and reporting

When reporting results, treat coordinate mode as part of the harness
configuration. In our experiments, Qwen 3.7-Plus was evaluated with:

```text
WEAVEBENCH_COMPUTER_COORD_MODE=auto  # resolves to norm1000 for Qwen
```

This is why run names often include `norm1000`. It documents that the GUI action
coordinates used a normalized model-facing coordinate frame, while the underlying
desktop and task environment remained unchanged.
