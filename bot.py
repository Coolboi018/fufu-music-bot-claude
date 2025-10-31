import discord
from discord.ext import commands
import asyncio
import os
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from collections import deque
import re
from aiohttp import web

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# Spotify setup
spotify = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=os.getenv('SPOTIFY_CLIENT_ID'),
    client_secret=os.getenv('SPOTIFY_CLIENT_SECRET')
))

# YT-DLP options
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': False,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'extract_flat': 'in_playlist'
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

class MusicQueue:
    def __init__(self):
        self.queue = deque()
        self.loop = False
        self.current = None
        self.voice_client = None
        self.inactivity_task = None
        
    def add(self, song):
        self.queue.append(song)
        
    def get_next(self):
        if self.loop and self.current:
            return self.current
        if self.queue:
            self.current = self.queue.popleft()
            return self.current
        return None
        
    def clear(self):
        self.queue.clear()
        
    def skip(self):
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()

# Store queues per guild
music_queues = {}

def get_queue(guild_id):
    if guild_id not in music_queues:
        music_queues[guild_id] = MusicQueue()
    return music_queues[guild_id]

async def inactivity_check(guild_id):
    """Auto-leave after 5 minutes of inactivity"""
    await asyncio.sleep(300)  # 5 minutes
    queue = get_queue(guild_id)
    if queue.voice_client and not queue.voice_client.is_playing():
        await queue.voice_client.disconnect()
        queue.voice_client = None

def extract_spotify_info(url):
    """Extract track info from Spotify URL"""
    if 'track' in url:
        track_id = url.split('track/')[-1].split('?')[0]
        track = spotify.track(track_id)
        return [{
            'title': f"{track['artists'][0]['name']} - {track['name']}",
            'url': f"ytsearch:{track['artists'][0]['name']} {track['name']}"
        }]
    elif 'playlist' in url:
        playlist_id = url.split('playlist/')[-1].split('?')[0]
        results = spotify.playlist_tracks(playlist_id)
        tracks = []
        for item in results['items']:
            track = item['track']
            tracks.append({
                'title': f"{track['artists'][0]['name']} - {track['name']}",
                'url': f"ytsearch:{track['artists'][0]['name']} {track['name']}"
            })
        return tracks
    elif 'album' in url:
        album_id = url.split('album/')[-1].split('?')[0]
        results = spotify.album_tracks(album_id)
        album = spotify.album(album_id)
        tracks = []
        for track in results['items']:
            tracks.append({
                'title': f"{track['artists'][0]['name']} - {track['name']}",
                'url': f"ytsearch:{track['artists'][0]['name']} {track['name']}"
            })
        return tracks
    return None

async def search_song(query):
    """Search for a song and return info"""
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
        
        if 'entries' in data:
            # Playlist or search results
            if query.startswith('ytsearch:'):
                # Take first result from search
                data = data['entries'][0]
            else:
                # YouTube playlist
                return [{'title': entry.get('title', 'Unknown'), 'url': entry.get('url', entry.get('webpage_url'))} 
                        for entry in data['entries'] if entry]
        
        return [{
            'title': data.get('title', 'Unknown'),
            'url': data.get('url', data.get('webpage_url'))
        }]
    except Exception as e:
        print(f"Error searching song: {e}")
        return None

async def play_next(guild_id):
    """Play the next song in queue"""
    queue = get_queue(guild_id)
    
    if not queue.voice_client:
        print("No voice client available")
        return
        
    song = queue.get_next()
    
    if song:
        try:
            # Cancel previous inactivity check
            if queue.inactivity_task:
                queue.inactivity_task.cancel()
            
            print(f"Attempting to play: {song['title']}")
            print(f"URL: {song['url']}")
            
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(song['url'], download=False))
            
            if 'entries' in data:
                data = data['entries'][0]
            
            url = data['url']
            print(f"Stream URL obtained: {url[:100]}...")
            
            def after_playing(error):
                if error:
                    print(f"Error playing: {error}")
                else:
                    print("Finished playing song")
                asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)
            
            source = discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)
            queue.voice_client.play(source, after=after_playing)
            print(f"Now playing: {song['title']}")
            
        except Exception as e:
            print(f"Error in play_next: {e}")
            import traceback
            traceback.print_exc()
            asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)
    else:
        print("No more songs in queue")
        # Start inactivity timer
        queue.inactivity_task = asyncio.create_task(inactivity_check(guild_id))

@bot.event
async def on_ready():
    print(f'{bot.user} is online!')
    await bot.change_presence(activity=discord.Game(name="!commands for help"))

@bot.command()
async def play(ctx, *, query: str):
    """Play a song from YouTube or Spotify"""
    if not ctx.author.voice:
        await ctx.send("❌ You need to be in a voice channel!")
        return
    
    queue = get_queue(ctx.guild.id)
    
    # Connect to voice channel if not connected
    if not queue.voice_client:
        try:
            queue.voice_client = await ctx.author.voice.channel.connect()
            print(f"Connected to voice channel: {ctx.author.voice.channel.name}")
        except Exception as e:
            await ctx.send(f"❌ Failed to connect to voice channel: {e}")
            print(f"Voice connection error: {e}")
            return
    
    await ctx.send(f"🔍 Searching for: **{query}**")
    
    songs = None
    
    # Check if Spotify URL
    if 'spotify.com' in query:
        try:
            songs = extract_spotify_info(query)
            print(f"Extracted {len(songs)} songs from Spotify")
        except Exception as e:
            await ctx.send(f"❌ Error processing Spotify link: {e}")
            print(f"Spotify error: {e}")
            return
    else:
        # YouTube URL or search
        if not query.startswith('http'):
            query = f"ytsearch:{query}"
        try:
            songs = await search_song(query)
            print(f"Found songs: {songs}")
        except Exception as e:
            await ctx.send(f"❌ Error searching: {e}")
            print(f"Search error: {e}")
            return
    
    if not songs:
        await ctx.send("❌ Could not find any songs!")
        return
    
    # Add songs to queue
    for song in songs:
        queue.add(song)
    
    if len(songs) > 1:
        await ctx.send(f"✅ Added **{len(songs)}** songs to the queue!")
    else:
        await ctx.send(f"✅ Added to queue: **{songs[0]['title']}**")
    
    # Start playing if not already playing
    if not queue.voice_client.is_playing():
        print("Starting playback...")
        await play_next(ctx.guild.id)

@bot.command()
async def pause(ctx):
    """Pause the current song"""
    queue = get_queue(ctx.guild.id)
    if queue.voice_client and queue.voice_client.is_playing():
        queue.voice_client.pause()
        await ctx.send("⏸️ Paused")
    else:
        await ctx.send("❌ Nothing is playing!")

@bot.command()
async def resume(ctx):
    """Resume the paused song"""
    queue = get_queue(ctx.guild.id)
    if queue.voice_client and queue.voice_client.is_paused():
        queue.voice_client.resume()
        await ctx.send("▶️ Resumed")
    else:
        await ctx.send("❌ Nothing is paused!")

@bot.command()
async def skip(ctx):
    """Skip the current song"""
    queue = get_queue(ctx.guild.id)
    if queue.voice_client and queue.voice_client.is_playing():
        queue.skip()
        await ctx.send("⏭️ Skipped")
    else:
        await ctx.send("❌ Nothing is playing!")

@bot.command()
async def stop(ctx):
    """Stop playing and clear the queue"""
    queue = get_queue(ctx.guild.id)
    queue.clear()
    queue.loop = False
    if queue.voice_client:
        queue.voice_client.stop()
    await ctx.send("⏹️ Stopped and cleared queue")

@bot.command()
async def leave(ctx):
    """Disconnect from voice channel"""
    queue = get_queue(ctx.guild.id)
    if queue.voice_client:
        await queue.voice_client.disconnect()
        queue.voice_client = None
        queue.clear()
        await ctx.send("👋 Left voice channel")
    else:
        await ctx.send("❌ Not in a voice channel!")

@bot.command()
async def loop(ctx):
    """Toggle loop for current song"""
    queue = get_queue(ctx.guild.id)
    queue.loop = not queue.loop
    status = "enabled" if queue.loop else "disabled"
    await ctx.send(f"🔁 Loop {status}")

@bot.command()
async def queue(ctx):
    """Show the current queue"""
    queue = get_queue(ctx.guild.id)
    
    if not queue.current and not queue.queue:
        await ctx.send("❌ Queue is empty!")
        return
    
    embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.blue())
    
    if queue.current:
        embed.add_field(name="Now Playing", value=f"🎶 {queue.current['title']}", inline=False)
    
    if queue.queue:
        queue_list = "\n".join([f"{i+1}. {song['title']}" for i, song in enumerate(list(queue.queue)[:10])])
        if len(queue.queue) > 10:
            queue_list += f"\n... and {len(queue.queue) - 10} more"
        embed.add_field(name="Up Next", value=queue_list, inline=False)
    
    if queue.loop:
        embed.set_footer(text="🔁 Loop is enabled")
    
    await ctx.send(embed=embed)

@bot.command(name='commands')
async def show_commands(ctx):
    """Show all commands"""
    embed = discord.Embed(
        title="🎵 Music Bot Commands",
        description="Play music from YouTube and Spotify!",
        color=discord.Color.green()
    )
    
    commands_list = {
        "!play <song/url>": "Play a song or playlist from YouTube/Spotify",
        "!pause": "Pause the current song",
        "!resume": "Resume the paused song",
        "!skip": "Skip to the next song",
        "!stop": "Stop playing and clear queue",
        "!leave": "Disconnect from voice channel",
        "!loop": "Toggle loop for current song",
        "!queue": "Show current queue",
        "!test": "Test voice connection",
        "!commands": "Show this message"
    }
    
    for cmd, desc in commands_list.items():
        embed.add_field(name=cmd, value=desc, inline=False)
    
    embed.set_footer(text="Auto-leaves after 5 minutes of inactivity")
    await ctx.send(embed=embed)

@bot.command()
async def test(ctx):
    """Test voice connection"""
    if not ctx.author.voice:
        await ctx.send("❌ Join a voice channel first!")
        return
    
    queue = get_queue(ctx.guild.id)
    
    if not queue.voice_client:
        queue.voice_client = await ctx.author.voice.channel.connect()
    
    info = f"""
    **Voice Connection Test:**
    Connected: {queue.voice_client.is_connected()}
    Playing: {queue.voice_client.is_playing()}
    Paused: {queue.voice_client.is_paused()}
    Latency: {queue.voice_client.latency}ms
    Channel: {queue.voice_client.channel.name}
    """
    await ctx.send(info)
    print(f"Voice client info: {info}")

# Run the bot
async def main():
    # Start simple web server for Render health checks
    app = web.Application()
    
    async def health_check(request):
        return web.Response(text="Bot is running!")
    
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv('PORT', 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    print(f"Web server started on port {port}")
    
    # Start the bot
    await bot.start(os.getenv('DISCORD_TOKEN'))

if __name__ == "__main__":
    asyncio.run(main())
