#!/bin/sh
# Rebuilds tests/data/ntfs.img.gz (a small NTFS volume for tests/test_ntfs.py). Needs ntfs-3g and FUSE (Linux).
set -e
cd "$(dirname "$0")"
work=$(mktemp -d)
truncate -s 8M "$work/img"
mkntfs -q -F -f -L test "$work/img"
mkdir "$work/m"
ntfs-3g "$work/img" "$work/m"
cd "$work/m"
mkdir -p "Pictures/Holiday 2024" "Pictures/Ünïcode – 日本" Program/App '$Recycle.Bin/S-1-5-21' Deep/a/b/c node_modules/x
echo hi > "Pictures/Holiday 2024/small.jpg"              # small enough to live inside the MFT record
head -c 123456 /dev/zero > Pictures/b.png
head -c 300000 /dev/zero > "Pictures/Ünïcode – 日本/clip.mp4"
touch Program/App/x.dll Program/App/y.jpg '$Recycle.Bin/S-1-5-21/$Rabc.jpg' node_modules/x/logo.png .dotfile.jpg
head -c 70000 /dev/zero > Deep/a/b/c/deep.heic
echo x > Deep/notes.txt
head -c 9000 /dev/zero > Deep/old.jxl
touch -d "2020-05-01 12:34:56" Pictures/b.png
cd - >/dev/null
fusermount -u "$work/m"
gzip -9 -c "$work/img" > ntfs.img.gz
rm -rf "$work"
