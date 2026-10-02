# ytaudio

Music cog for Red-DiscordBot using yt-dlp instead of Lavalink. Plays YouTube, SoundCloud and Spotify links.

## Install
Requires FFmpeg.
```
[p]repo add ytdl-audio <repo url>
[p]cog install ytdl-audio ytaudio
[p]unload audio
[p]load ytaudio
```
Restart the bot once after the first install.

## Commands
```
play <url|search>     play or queue something
bumpplay <url|search> queue it next
search <text>         pick from 10 results (sc <text> for soundcloud)
skip [n]              skip, or jump to queue position n
prev                  play the last track again
pause / resume
stop                  stop, clear queue, leave
seek <1:30|+30|-10>
volume [0-150]
np                    now playing
queue                 show queue
queue clear|shuffle|clean|cleanself|search <text>
remove <n|@user>
bump <n>              move track n to the front
repeat / shuffle / autoplay   toggles
summon / dc

playlist create <name> [url]
playlist append <name> <url|search>
playlist queue <name>          save current queue
playlist start <name>
playlist list / info / rename / remove / delete

audioset settings
audioset dj / role <role> / vote <percent>
audioset emptydisconnect <sec> / dc / maxlength <sec> / maxvolume / notify
audioset ytdlp update / autoupdate / jsruntime    (owner)
```
