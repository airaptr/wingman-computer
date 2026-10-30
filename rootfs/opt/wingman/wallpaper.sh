#!/bin/sh
# Paints the Wingman wallpaper: a deep blue to slate gradient, drawn by hsetroot (no image file).
hsetroot -add "#0f1b2d" -add "#1f3b5c" -add "#4a6d8c" -gradient 160 || exit 1
exec sleep infinity
