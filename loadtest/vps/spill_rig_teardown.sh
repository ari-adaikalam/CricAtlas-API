T=/srv/cricket/testrun
docker rm -f cricket-spill-test >/dev/null 2>&1; echo "container removed: $(docker ps -a --format '{{.Names}}' | grep -c spill)  left"
umount $T/scratch && echo "unmounted"
shred -u $T/scratch.img && echo "image shredded and removed"
rmdir $T/scratch $T && echo "folders removed"
ls /srv/cricket; findmnt -n -o TARGET | grep -c testrun | sed 's/^/test mounts left: /'
echo "host avail $(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo) MiB | root free $(df -BG --output=avail / | tail -1 | tr -d ' ')"
