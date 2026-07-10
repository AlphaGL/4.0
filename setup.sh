#!/bin/bash

# install dependancies
pip install setuptools
pip install -r requirements.txt

# Run django commands
# NOTE: never run makemigrations on the server — migrations are created locally
# and committed. Running it here can generate phantom migrations and cause DB drift.
python manage.py migrate --noinput
python manage.py collectstatic --noinput