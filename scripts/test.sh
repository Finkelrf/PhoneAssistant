#!/bin/sh
set -e
python -m pytest -q -p no:cacheprovider tests "$@"
