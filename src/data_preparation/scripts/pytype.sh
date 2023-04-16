#!/bin/bash
if [ ! -d "$1"/pytype/ ]; then
    echo Running: pytype -V3.6 --keep-going -o ./pytype -P $SITE_PACKAGES:"$1" infer "$1"
    pytype -V3.6 --keep-going -o ./pytype -P $SITE_PACKAGES:"$1" infer "$1"
fi

files=$(find "$1" -name "*.py")
for f in $files; do
    f_stub=$f"i"
    f_stub="./pytype/pyi"${f_stub#"$1"}
    if [ -f "$f_stub" ]; then
        echo Running: merge-pyi -i "$f" "$f_stub"
        merge-pyi -i "$f" "$f_stub"
    fi
done