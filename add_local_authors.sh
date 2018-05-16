#!/usr/bin/env bash

cd run
source bin/activate
python -c 'import webdb ; webdb.add_local_authors("/home/grad/astroph-coffee/local_authors.csv")'