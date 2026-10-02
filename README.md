# ytaudio

Music cog for Red-DiscordBot. Uses yt-dlp instead of Lavalink.

## Install
Requires FFmpeg.
```
[p]repo add ytdl-audio <repo url>
[p]cog install ytdl-audio ytaudio
[p]unload audio
[p]load ytaudio
```
Restart the bot once after the first install. On first load yt-dlp installs itself (takes a minute).

To go back to Red's Audio: `[p]unload ytaudio`, `[p]load audio`.

## What it plays
- YouTube, YouTube Music, SoundCloud and most sites yt-dlp supports
- Links to videos and playlists, or just type a song name
- Spotify tracks, albums and playlists. The song is looked up on YouTube Music and played from there. Spotify playlists are limited to 100 tracks.

Some audiobooks are only on YouTube Music Premium and can't be played.

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
repeat                loop the queue
shuffle               play the queue in random order
autoplay              keep playing similar songs when the queue ends
summon                move the bot to your channel
dc                    leave
percent               who queued how much
```

## Playlists
Saved per server. Anyone can create and start them, only the creator or a mod can change them.
```
playlist create <name> [url]          empty, or from a link
playlist append <name> <url|search>
playlist queue <name>                 save what's playing now
playlist start <name>
playlist list
playlist info <name>
playlist remove <name> <n>
playlist rename <name> <new>
playlist delete <name>
```

## Permissions
Everyone can control the music, as long as they're in the bot's voice channel. Mods can do it from anywhere.

DJ mode limits control to a role. With vote skip on, others can still vote to skip.

## Settings
Admins only.
```
audioset settings                show everything
audioset role <role>             set the DJ role
audioset dj                      DJ mode on/off
audioset vote <percent>          vote skip, 0 = off
audioset emptydisconnect <sec>   leave when alone, 0 = off (default 300)
audioset dc                      leave when the queue ends
audioset maxlength <sec>         max track length, 0 = off
audioset maxvolume <percent>     default 150, up to 200
audioset notify                  "Now Playing" messages on/off
```

Bot owner:
```
audioset ytdlp update            update yt-dlp now
audioset ytdlp autoupdate        daily update on/off (default on)
audioset ytdlp jsruntime [x]     e.g. deno:/usr/bin/deno, empty = auto
audioset ytdlp path [x]          use another yt-dlp, empty = built-in
audiostats                       where the bot is playing
```

## Notes
- yt-dlp lives in the cog's data folder and updates itself daily. No restart needed.
- YouTube needs a JS runtime for yt-dlp. deno, node or bun is found automatically.
- The next track is loaded in the background, so skipping is instant.
- Spotify API keys (`[p]set api spotify`) are used if they work, otherwise none are needed.
