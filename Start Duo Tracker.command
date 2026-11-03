#!/usr/bin/env bash
# macOS: double-click to start watching. Keeps the Mac awake while it runs.
cd "$(dirname "$0")"
exec caffeinate -dims ./duo watch
