#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
echo "知新 · 每天几篇，读有所得"
exec python3 app.py
