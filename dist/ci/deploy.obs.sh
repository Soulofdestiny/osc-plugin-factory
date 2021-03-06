#!/bin/bash

cat << eom > ~/.oscrc
[general]
apiurl = https://api.opensuse.org
[https://api.opensuse.org]
user = $OBS_USER
pass = $OBS_PASS
email = $OBS_EMAIL
eom

osc checkout "$OBS_PACKAGE"
cd "$OBS_PACKAGE"

rm *.obscpio
osc service disabledrun
echo >> _servicedata
osc addremove
osc commit -m "$(grep -oP 'version: \K.*' *.obsinfo)"
