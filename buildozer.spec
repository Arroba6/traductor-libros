[app]
title = Traductor de Libros
package.name = traductorlibros
package.domain = io.github.arroba6
source.dir = .
source.include_exts = py,png,jpg,kv,json
source.exclude_dirs = bin,.buildozer,.github
version = 1.0
requirements = python3,kivy==2.3.0,beautifulsoup4,soupsieve,certifi,openssl,pyjnius,android
orientation = portrait
fullscreen = 0

android.permissions = INTERNET,WAKE_LOCK
android.api = 34
android.minapi = 29
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = True

p4a.branch = v2024.01.21

[buildozer]
log_level = 2
warn_on_root = 0
