#! /bin/sh
# Publisher IOC: empad_ioc.py replaces the old python_epics_ioc.py (now in
# original_version/). Needs ad_ioc_base.py alongside it (deployed separately).
python empad_ioc.py 2>../error_epics.txt

