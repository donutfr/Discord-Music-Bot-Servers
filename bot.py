import random
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import urllib.request
import urllib.parse
import json
import re
import os

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable not set!")

# ─── Bot Setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        guild = discord.Object(id=1486393856667943074)
        # Copy all commands to guild first, then sync
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        # Clear global so no doubles show
        self.tree.clear_commands(guild=None)
        await self.tree.sync(guild=None)
        print("✅ Slash commands synced.")

bot = Bot()

# ─── Music Queues ─────────────────────────────────────────────────────────────
queues = {}

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = {"songs": [], "voice_client": None, "volume": 0.5, "bass_boost": 0, "effect": "none", "restarting": False}
    return queues[guild_id]

EFFECTS = {
    "none":     None,
    "8d":       "apulsator=hz=0.125",
    "nightcore": "aresample=48000,asetrate=48000*1.25",
    "slowed":   "aresample=48000,asetrate=48000*0.8",
    "vaporwave": "aresample=48000,asetrate=48000*0.8,atempo=1.0",
    "echo":     "aecho=0.8:0.9:1000:0.3",
    "reverb":   "aecho=0.8:0.88:60:0.4",
    "earrape":  "acrusher=level_in=8:level_out=18:bits=8:mode=log:aa=1",
    "underwater": "lowpass=f=800,aecho=0.8:0.9:1000:0.3",
    "robot":    "afftfilt=real='hypot(re,im)*sin(0)':imag='hypot(re,im)*cos(0)':win_size=512:overlap=0.75",
}

def build_ffmpeg_options(bass_boost=0, effect="none"):
    filters = []
    if bass_boost and bass_boost > 0:
        filters.append(f"bass=g={bass_boost}")
    if effect and effect != "none" and effect in EFFECTS and EFFECTS[effect]:
        filters.append(EFFECTS[effect])
    before = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    if filters:
        return {"before_options": before, "options": f"-vn -af {','.join(filters)}"}
    return {"before_options": before, "options": "-vn"}

def can_control_interaction(interaction, queue):
    if not queue["songs"]:
        return True
    requester_id = queue["songs"][0].get("requester_id")
    if requester_id is None:
        return True
    if interaction.user.id == requester_id:
        return True
    if interaction.user.guild_permissions.administrator:
        return True
    return False

async def search_yt(query):
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        try:
            if not query.startswith("http"):
                query = f"ytsearch:{query}"
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                info = info["entries"][0]
            duration_secs = info.get("duration", 0)
            mins, secs = divmod(int(duration_secs), 60)
            return {
                "title": info.get("title", "Unknown"),
                "url": info.get("webpage_url", ""),
                "stream_url": info.get("url", ""),
                "duration": f"{mins}:{secs:02d}",
                "thumbnail": info.get("thumbnail", ""),
            }
        except Exception as e:
            print(f"yt_dlp error: {e}")
            return None


async def fetch_playlist(url: str):
    """Fetch all tracks from a YouTube playlist URL."""
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "extract_flat": "in_playlist",
        "source_address": "0.0.0.0",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            if "entries" not in info:
                return None
            songs = []
            for entry in info["entries"]:
                if not entry:
                    continue
                video_url = entry.get("url") or entry.get("webpage_url") or f"https://www.youtube.com/watch?v={entry.get('id', '')}"
                duration_secs = entry.get("duration", 0) or 0
                mins, secs = divmod(int(duration_secs), 60)
                songs.append({
                    "title": entry.get("title", "Unknown"),
                    "url": video_url,
                    "stream_url": None,  # fetched lazily when played
                    "duration": f"{mins}:{secs:02d}",
                    "thumbnail": entry.get("thumbnail", "") or "",
                })
            return songs, info.get("title", "Playlist")
        except Exception as e:
            print(f"Playlist fetch error: {e}")
            return None

async def play_next(guild, text_channel):
    queue = get_queue(guild.id)
    if not queue["songs"]:
        await text_channel.send("📭 Queue finished!")
        if queue["voice_client"]:
            await queue["voice_client"].disconnect()
        queues.pop(guild.id, None)
        return

    song = queue["songs"][0]
    try:
        # Lazily fetch stream URL for playlist tracks
        if not song.get("stream_url"):
            fresh = await search_yt(song["url"])
            if fresh:
                song["stream_url"] = fresh["stream_url"]
                if not song.get("thumbnail"):
                    song["thumbnail"] = fresh.get("thumbnail", "")
            else:
                await text_channel.send(f"⚠️ Skipping **{song['title']}** — could not load stream.")
                queue["songs"].pop(0)
                await play_next(guild, text_channel)
                return
        ffmpeg_opts = build_ffmpeg_options(queue["bass_boost"], queue.get("effect", "none"))
        source = discord.FFmpegPCMAudio(song["stream_url"], **ffmpeg_opts)
        source = discord.PCMVolumeTransformer(source, volume=queue["volume"])

        def after_play(error):
            if error:
                print(f"Player error: {error}")
            if queue.get("restarting"):
                return
            queue["songs"].pop(0)
            asyncio.run_coroutine_threadsafe(play_next(guild, text_channel), bot.loop)

        queue["voice_client"].play(source, after=after_play)

        boost_tag = f" 🎸 +{queue['bass_boost']}dB" if queue["bass_boost"] else ""
        effect_tag = f" ✨ {queue.get('effect', 'none').title()}" if queue.get("effect", "none") != "none" else ""
        embed = discord.Embed(
            title=f"🎵 Now Playing{boost_tag}{effect_tag}",
            description=f"**[{song['title']}]({song['url']})**",
            color=0x1DB954,
        )
        embed.add_field(name="Duration", value=song["duration"], inline=True)
        embed.add_field(name="In queue", value=str(len(queue["songs"])), inline=True)
        if song["thumbnail"]:
            embed.set_thumbnail(url=song["thumbnail"])
        await text_channel.send(embed=embed)

    except Exception as e:
        print(f"play_next error: {e}")
        await text_channel.send("❌ Error playing that song, skipping...")
        queue["songs"].pop(0)
        await play_next(guild, text_channel)

# ─── Points / Coins ───────────────────────────────────────────────────────────
points = {}

def get_points(user_id):
    return points.get(user_id, 0)

def add_points(user_id, amount):
    points[user_id] = points.get(user_id, 0) + amount

def remove_points(user_id, amount):
    points[user_id] = max(0, points.get(user_id, 0) - amount)

# ─── Trivia Data ──────────────────────────────────────────────────────────────
TRIVIA = [
    # Geography
    {"q": "What is the capital of France?", "a": "paris", "opts": ["London", "Berlin", "Paris", "Madrid"]},
    {"q": "What is the largest ocean on Earth?", "a": "pacific", "opts": ["Atlantic", "Indian", "Arctic", "Pacific"]},
    {"q": "What is the smallest country in the world?", "a": "vatican", "opts": ["Monaco", "Vatican City", "San Marino", "Liechtenstein"]},
    {"q": "Which country has the most natural lakes?", "a": "canada", "opts": ["USA", "Russia", "Canada", "Brazil"]},
    {"q": "What is the longest river in the world?", "a": "nile", "opts": ["Amazon", "Nile", "Yangtze", "Mississippi"]},
    {"q": "Which continent is the Sahara Desert on?", "a": "africa", "opts": ["Asia", "Australia", "Africa", "South America"]},
    {"q": "What is the capital of Japan?", "a": "tokyo", "opts": ["Osaka", "Kyoto", "Tokyo", "Hiroshima"]},
    {"q": "Which country is home to the kangaroo?", "a": "australia", "opts": ["New Zealand", "South Africa", "Australia", "Brazil"]},
    {"q": "What is the capital of Canada?", "a": "ottawa", "opts": ["Toronto", "Vancouver", "Montreal", "Ottawa"]},
    {"q": "Which is the largest country by area?", "a": "russia", "opts": ["China", "USA", "Canada", "Russia"]},
    # Science
    {"q": "How many sides does a hexagon have?", "a": "6", "opts": ["5", "6", "7", "8"]},
    {"q": "What planet is known as the Red Planet?", "a": "mars", "opts": ["Venus", "Jupiter", "Mars", "Saturn"]},
    {"q": "What is the chemical symbol for gold?", "a": "au", "opts": ["Go", "Gd", "Au", "Ag"]},
    {"q": "How many bones are in the adult human body?", "a": "206", "opts": ["196", "206", "216", "226"]},
    {"q": "What element does O represent on the periodic table?", "a": "oxygen", "opts": ["Osmium", "Oxygen", "Oganesson", "Oxide"]},
    {"q": "What is the smallest planet in our solar system?", "a": "mercury", "opts": ["Pluto", "Mars", "Mercury", "Venus"]},
    {"q": "What is the speed of light (approx) in km/s?", "a": "300000", "opts": ["150000", "300000", "500000", "1000000"]},
    {"q": "How many chromosomes do humans have?", "a": "46", "opts": ["23", "46", "48", "52"]},
    {"q": "What is the powerhouse of the cell?", "a": "mitochondria", "opts": ["Nucleus", "Ribosome", "Mitochondria", "Vacuole"]},
    {"q": "What gas do plants absorb from the atmosphere?", "a": "carbon dioxide", "opts": ["Oxygen", "Nitrogen", "Carbon Dioxide", "Hydrogen"]},
    {"q": "What is the hardest natural substance on Earth?", "a": "diamond", "opts": ["Gold", "Iron", "Diamond", "Quartz"]},
    {"q": "How many planets are in our solar system?", "a": "8", "opts": ["7", "8", "9", "10"]},
    {"q": "What is H2O commonly known as?", "a": "water", "opts": ["Hydrogen", "Oxygen", "Water", "Salt"]},
    {"q": "Which planet has the most moons?", "a": "saturn", "opts": ["Jupiter", "Saturn", "Uranus", "Neptune"]},
    # History
    {"q": "In what year did World War II end?", "a": "1945", "opts": ["1943", "1944", "1945", "1946"]},
    {"q": "Who painted the Mona Lisa?", "a": "da vinci", "opts": ["Picasso", "Da Vinci", "Michelangelo", "Raphael"]},
    {"q": "Who wrote Romeo and Juliet?", "a": "shakespeare", "opts": ["Dickens", "Shakespeare", "Austen", "Hemingway"]},
    {"q": "In what year did the Titanic sink?", "a": "1912", "opts": ["1910", "1912", "1915", "1920"]},
    {"q": "Who was the first US President?", "a": "washington", "opts": ["Lincoln", "Jefferson", "Washington", "Adams"]},
    {"q": "In what year did World War I begin?", "a": "1914", "opts": ["1912", "1914", "1916", "1918"]},
    {"q": "Who discovered penicillin?", "a": "fleming", "opts": ["Pasteur", "Fleming", "Curie", "Darwin"]},
    {"q": "The Great Wall of China was built to keep out which group?", "a": "mongols", "opts": ["Japanese", "Mongols", "Russians", "Persians"]},
    {"q": "Who was the first person to walk on the moon?", "a": "armstrong", "opts": ["Buzz Aldrin", "Neil Armstrong", "Yuri Gagarin", "John Glenn"]},
    {"q": "In what year did the Berlin Wall fall?", "a": "1989", "opts": ["1985", "1987", "1989", "1991"]},
    # Pop Culture
    {"q": "How many strings does a standard guitar have?", "a": "6", "opts": ["4", "5", "6", "7"]},
    {"q": "Which band sang Bohemian Rhapsody?", "a": "queen", "opts": ["The Beatles", "Queen", "Led Zeppelin", "ABBA"]},
    {"q": "What is the best-selling video game of all time?", "a": "minecraft", "opts": ["Tetris", "GTA V", "Minecraft", "Wii Sports"]},
    {"q": "Which artist released the album Thriller?", "a": "michael jackson", "opts": ["Prince", "Michael Jackson", "Madonna", "David Bowie"]},
    {"q": "How many players are on a standard soccer team?", "a": "11", "opts": ["9", "10", "11", "12"]},
    {"q": "What sport is played at Wimbledon?", "a": "tennis", "opts": ["Cricket", "Golf", "Tennis", "Polo"]},
    {"q": "Which movie features the quote To infinity and beyond?", "a": "toy story", "opts": ["A Bug's Life", "Toy Story", "Finding Nemo", "Cars"]},
    {"q": "What color is the sky on a clear day?", "a": "blue", "opts": ["Green", "Blue", "White", "Purple"]},
    # Math
    {"q": "What is 7 x 8?", "a": "56", "opts": ["48", "54", "56", "64"]},
    {"q": "What is the square root of 144?", "a": "12", "opts": ["10", "11", "12", "14"]},
    {"q": "What is 15% of 200?", "a": "30", "opts": ["20", "25", "30", "35"]},
    {"q": "How many seconds are in an hour?", "a": "3600", "opts": ["1800", "3000", "3600", "4200"]},
    {"q": "What is the fastest land animal?", "a": "cheetah", "opts": ["Lion", "Horse", "Cheetah", "Falcon"]},
    {"q": "Which country invented pizza?", "a": "italy", "opts": ["Greece", "Spain", "France", "Italy"]},
]

active_trivia = {}

EIGHT_BALL = [
    "It is certain.", "It is decidedly so.", "Without a doubt.", "Yes, definitely!",
    "You may rely on it.", "As I see it, yes.", "Most likely.", "Outlook good.",
    "Yes.", "Signs point to yes.", "Reply hazy, try again.", "Ask again later.",
    "Better not tell you now.", "Cannot predict now.", "Concentrate and ask again.",
    "Don't count on it.", "My reply is no.", "My sources say no.",
    "Outlook not so good.", "Very doubtful.",
]

JOKES = [
    ("Why don't scientists trust atoms?", "Because they make up everything!"),
    ("Why did the scarecrow win an award?", "Because he was outstanding in his field!"),
    ("I told my wife she was drawing her eyebrows too high.", "She looked surprised."),
    ("Why can't you give Elsa a balloon?", "Because she'll let it go!"),
    ("What do you call fake spaghetti?", "An impasta!"),
    ("Why did the bicycle fall over?", "Because it was two-tired!"),
    ("I'm reading a book about anti-gravity.", "It's impossible to put down!"),
    ("What do you call cheese that isn't yours?", "Nacho cheese!"),
]

ROASTS = [
    "I'd roast you, but my mom said I'm not allowed to burn trash.",
    "You're the reason the gene pool needs a lifeguard.",
    "I'd explain it to you, but I left my crayons at home.",
    "You're not stupid; you just have bad luck thinking.",
    "If laughter is the best medicine, your face must be curing diseases.",
    "I've seen better heads on a glass of root beer.",
]

POLL_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]

# ─── Events ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening, name="/help | Fun & Music"
    ))

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.channel.id in active_trivia:
        data = active_trivia[message.channel.id]
        ans = message.content.strip().upper()
        if ans in data["letters"]:
            idx = data["letters"].index(ans)
            chosen = data["shuffled"][idx].lower()
            correct_a = data["question"]["a"]
            is_correct = correct_a in chosen or chosen in correct_a
            data["task"].cancel()
            active_trivia.pop(message.channel.id)
            if is_correct:
                earned = random.randint(1, 10)
                add_points(message.author.id, earned)
                total = get_points(message.author.id)
                await message.reply(f"🎉 **Correct!** {data['shuffled'][idx]} is right! Well done, {message.author.mention}!\n🪙 You earned **{earned} coins**! Total: **{total} coins**")
            else:
                correct = next((o for o in data["shuffled"] if correct_a in o.lower() or o.lower() in correct_a), correct_a)
                await message.reply(f"❌ Wrong! The correct answer was **{correct}**. Better luck next time!")
    await bot.process_commands(message)

# ══════════════════════════════════════════════════════════════════════════════
# SLASH COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

# ─── Help ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="help", description="Show all bot commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="🤖 Bot Commands", color=0x5865F2)
    embed.add_field(name="🎵 Music", value="\n".join([
        "`/play` — Play a song or add to queue",
        "`/skip` — Skip current song",
        "`/stop` — Stop music & clear queue",
        "`/queue` — View the song queue",
        "`/pause` — Pause the music",
        "`/resume` — Resume the music",
        "`/volume` — Set volume (1-100)",
        "`/np` — Show current song",
        "`/bassboost` — Set bass boost level",
        "`/effect` — Apply audio effects (8D, nightcore, etc)",
        "`/lyrics` — Show lyrics",
    ]), inline=False)
    embed.add_field(name="🎮 Games & Fun", value="\n".join([
        "`/trivia` — Start a trivia question",
        "`/rps` — Play rock paper scissors",
        "`/8ball` — Ask the magic 8-ball",
        "`/poll` — Create a poll",
        "`/roll` — Roll a dice",
        "`/coinflip` — Flip a coin",
        "`/roast` — Roast someone",
        "`/joke` — Tell a random joke",
    ]), inline=False)
    embed.add_field(name="🪙 Coins & Gambling", value="\n".join([
        "`/coins` — Check coin balance",
        "`/leaderboard` — Top 10 coin holders",
        "`/gamble` — Gamble coins (45% win chance)",
        "`/coinflip_bet` — Bet coins on a coin flip",
    ]), inline=False)
    embed.add_field(name="📊 Info", value="\n".join([
        "`/ping` — Check bot latency",
        "`/serverinfo` — Server information",
        "`/userinfo` — User information",
    ]), inline=False)
    await interaction.response.send_message(embed=embed)

# ─── Info ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="ping", description="Check the bot's latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! Latency: **{round(bot.latency * 1000)}ms**")

@bot.tree.command(name="serverinfo", description="Show information about this server")
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    embed = discord.Embed(title=f"📊 {g.name}", color=0xFEE75C)
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="Owner", value=g.owner.mention, inline=True)
    embed.add_field(name="Members", value=str(g.member_count), inline=True)
    embed.add_field(name="Channels", value=str(len(g.channels)), inline=True)
    embed.add_field(name="Roles", value=str(len(g.roles)), inline=True)
    embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "D"), inline=True)
    embed.add_field(name="Boost Level", value=f"Level {g.premium_tier}", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="userinfo", description="Show information about a user")
@app_commands.describe(member="The user to look up (leave blank for yourself)")
async def userinfo(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    roles = [r.mention for r in member.roles if r.name != "@everyone"]
    embed = discord.Embed(title=f"👤 {member.display_name}", color=0x57F287)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=str(member.id), inline=True)
    embed.add_field(name="Joined Discord", value=discord.utils.format_dt(member.created_at, "D"), inline=True)
    embed.add_field(name="Joined Server", value=discord.utils.format_dt(member.joined_at, "D") if member.joined_at else "N/A", inline=True)
    embed.add_field(name="Roles", value=", ".join(roles) if roles else "None", inline=False)
    await interaction.response.send_message(embed=embed)

# ─── Fun ──────────────────────────────────────────────────────────────────────
@bot.tree.command(name="roll", description="Roll a dice")
@app_commands.describe(sides="Number of sides (default 6, max 1000)")
async def roll(interaction: discord.Interaction, sides: int = 6):
    if sides < 2 or sides > 1000:
        return await interaction.response.send_message("❌ Pick between 2 and 1000 sides.", ephemeral=True)
    await interaction.response.send_message(f"🎲 You rolled a **d{sides}** and got: **{random.randint(1, sides)}**")

@bot.tree.command(name="coinflip", description="Flip a coin")
async def coinflip(interaction: discord.Interaction):
    await interaction.response.send_message(f"🪙 The coin landed on: **{random.choice(['Heads', 'Tails'])}**!")

@bot.tree.command(name="8ball", description="Ask the magic 8-ball a question")
@app_commands.describe(question="Your yes/no question")
async def eight_ball(interaction: discord.Interaction, question: str):
    embed = discord.Embed(title="🎱 Magic 8-Ball", color=0x2C2F33)
    embed.add_field(name="❓ Question", value=question, inline=False)
    embed.add_field(name="🎱 Answer", value=random.choice(EIGHT_BALL), inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="joke", description="Tell a random joke")
async def joke(interaction: discord.Interaction):
    setup, punchline = random.choice(JOKES)
    embed = discord.Embed(title="😂 Joke Time!", description=f"**{setup}**\n\n||{punchline}||", color=0xEB459E)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="roast", description="Roast someone (all in good fun!)")
@app_commands.describe(member="Who to roast")
async def roast(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.send_message(f"{member.mention} {random.choice(ROASTS)} 😄 *(all in good fun!)*")

@bot.tree.command(name="rps", description="Play rock paper scissors against the bot")
@app_commands.describe(choice="Your choice")
@app_commands.choices(choice=[
    app_commands.Choice(name="Rock 🪨", value="rock"),
    app_commands.Choice(name="Paper 📄", value="paper"),
    app_commands.Choice(name="Scissors ✂️", value="scissors"),
])
async def rps(interaction: discord.Interaction, choice: str):
    emojis = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
    bot_choice = random.choice(["rock", "paper", "scissors"])
    if choice == bot_choice:
        result = "It's a tie!"
    elif (choice == "rock" and bot_choice == "scissors") or \
         (choice == "paper" and bot_choice == "rock") or \
         (choice == "scissors" and bot_choice == "paper"):
        result = "You win! 🎉"
    else:
        result = "I win! 😈"
    embed = discord.Embed(title="✊ Rock Paper Scissors", color=0xED4245)
    embed.add_field(name="Your pick", value=f"{emojis[choice]} {choice}", inline=True)
    embed.add_field(name="My pick", value=f"{emojis[bot_choice]} {bot_choice}", inline=True)
    embed.add_field(name="Result", value=result, inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="trivia", description="Start a trivia question — answer with A, B, C or D in chat")
async def trivia(interaction: discord.Interaction):
    if interaction.channel.id in active_trivia:
        return await interaction.response.send_message("⚠️ A trivia question is already active!", ephemeral=True)
    q = random.choice(TRIVIA)
    shuffled = q["opts"][:]
    random.shuffle(shuffled)
    letters = ["A", "B", "C", "D"]
    lines = "\n".join(f"{letters[i]}) {shuffled[i]}" for i in range(len(shuffled)))
    embed = discord.Embed(title="🧠 Trivia Time!", description=f"**{q['q']}**\n\n{lines}", color=0xFEE75C)
    embed.set_footer(text="Type A, B, C, or D in chat to answer! You have 20 seconds.")
    await interaction.response.send_message(embed=embed)

    async def expire():
        await asyncio.sleep(20)
        if interaction.channel.id in active_trivia:
            active_trivia.pop(interaction.channel.id)
            correct = next((o for o in shuffled if q["a"] in o.lower() or o.lower() in q["a"]), q["a"])
            await interaction.channel.send(f"⏰ Time's up! The correct answer was: **{correct}**")

    task = asyncio.create_task(expire())
    active_trivia[interaction.channel.id] = {"question": q, "shuffled": shuffled, "letters": letters, "task": task}

@bot.tree.command(name="poll", description="Create a poll with up to 5 options")
@app_commands.describe(question="The poll question", option1="First option", option2="Second option",
                       option3="Third option (optional)", option4="Fourth option (optional)", option5="Fifth option (optional)")
async def poll(interaction: discord.Interaction, question: str, option1: str, option2: str,
               option3: str = None, option4: str = None, option5: str = None):
    options = [o for o in [option1, option2, option3, option4, option5] if o]
    lines = "\n".join(f"{POLL_EMOJIS[i]} {options[i]}" for i in range(len(options)))
    embed = discord.Embed(title=f"📊 {question}", description=lines, color=0x5865F2)
    embed.set_footer(text=f"Poll by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    poll_msg = await interaction.original_response()
    for i in range(len(options)):
        await poll_msg.add_reaction(POLL_EMOJIS[i])

# ─── Coins & Gambling ─────────────────────────────────────────────────────────
@bot.tree.command(name="coins", description="Check your coin balance or someone else's")
@app_commands.describe(member="Whose balance to check (leave blank for yourself)")
async def coins(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    embed = discord.Embed(title="🪙 Coin Balance",
        description=f"{member.mention} has **{get_points(member.id)} coins**", color=0xFEE75C)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Show the top 10 coin holders")
async def leaderboard(interaction: discord.Interaction):
    if not points:
        return await interaction.response.send_message("📭 No one has earned coins yet! Play `/trivia` to get started.")
    sorted_players = sorted(points.items(), key=lambda x: x[1], reverse=True)[:10]
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (user_id, pts) in enumerate(sorted_players):
        medal = medals[i] if i < 3 else f"`{i+1}.`"
        user = interaction.guild.get_member(user_id)
        name = user.display_name if user else f"Unknown"
        lines.append(f"{medal} **{name}** — {pts} coins")
    embed = discord.Embed(title="🏆 Coin Leaderboard", description="\n".join(lines), color=0xFEE75C)
    embed.set_footer(text="Earn coins by answering trivia correctly!")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="gamble", description="Gamble your coins — 45% chance to double, 55% to lose")
@app_commands.describe(amount="How many coins to bet")
async def gamble(interaction: discord.Interaction, amount: int):
    balance = get_points(interaction.user.id)
    if amount <= 0:
        return await interaction.response.send_message("❌ Bet must be at least 1 coin.", ephemeral=True)
    if balance == 0:
        return await interaction.response.send_message("❌ You have no coins! Play `/trivia` to earn some.", ephemeral=True)
    if amount > balance:
        return await interaction.response.send_message(f"❌ You only have **{balance} coins**!", ephemeral=True)
    if random.random() < 0.45:
        add_points(interaction.user.id, amount)
        embed = discord.Embed(title="🎰 You Won!", color=0x57F287,
            description=f"🎉 You bet **{amount} coins** and won **{amount * 2} coins**!\nNew balance: **{get_points(interaction.user.id)} coins**")
    else:
        remove_points(interaction.user.id, amount)
        embed = discord.Embed(title="🎰 You Lost!", color=0xED4245,
            description=f"💸 You bet **{amount} coins** and lost them all.\nNew balance: **{get_points(interaction.user.id)} coins**")
    embed.set_footer(text="Win chance: 45% | Try again with /gamble")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="coinflip_bet", description="Bet coins on a coin flip")
@app_commands.describe(amount="How many coins to bet", side="Heads or tails?")
@app_commands.choices(side=[
    app_commands.Choice(name="Heads", value="heads"),
    app_commands.Choice(name="Tails", value="tails"),
])
async def coinflip_bet(interaction: discord.Interaction, amount: int, side: str):
    balance = get_points(interaction.user.id)
    if amount <= 0:
        return await interaction.response.send_message("❌ Bet must be at least 1 coin.", ephemeral=True)
    if balance == 0:
        return await interaction.response.send_message("❌ You have no coins! Play `/trivia` to earn some.", ephemeral=True)
    if amount > balance:
        return await interaction.response.send_message(f"❌ You only have **{balance} coins**!", ephemeral=True)
    result = random.choice(["heads", "tails"])
    if result == side:
        add_points(interaction.user.id, amount)
        embed = discord.Embed(title="🪙 Coin Flip — You Won!", color=0x57F287,
            description=f"Landed on **{result}**!\n🎉 You bet **{amount}** on {side} and won **{amount * 2} coins**!\nNew balance: **{get_points(interaction.user.id)} coins**")
    else:
        remove_points(interaction.user.id, amount)
        embed = discord.Embed(title="🪙 Coin Flip — You Lost!", color=0xED4245,
            description=f"Landed on **{result}**!\n💸 You bet **{amount}** on {side} and lost.\nNew balance: **{get_points(interaction.user.id)} coins**")
    await interaction.response.send_message(embed=embed)

# ─── Music ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="play", description="Play a song or add it to the queue")
@app_commands.describe(query="Song name or YouTube URL")
async def play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        return await interaction.response.send_message("❌ You need to be in a voice channel!", ephemeral=True)
    voice_channel = interaction.user.voice.channel
    queue = get_queue(interaction.guild.id)
    await interaction.response.send_message("🔍 Searching...")
    if queue["voice_client"] is None or not queue["voice_client"].is_connected():
        try:
            queue["voice_client"] = await voice_channel.connect()
        except Exception as e:
            return await interaction.edit_original_response(content=f"❌ Could not join voice channel: {e}")
    elif queue["voice_client"].channel != voice_channel:
        await queue["voice_client"].move_to(voice_channel)

    # Detect playlist
    is_playlist = ("list=" in query or "playlist" in query.lower()) and query.startswith("http")
    if is_playlist:
        await interaction.edit_original_response(content="📋 Loading playlist...")
        result = await fetch_playlist(query)
        if not result:
            return await interaction.edit_original_response(content="❌ Could not load that playlist. Make sure it's a public YouTube playlist URL!")
        songs, playlist_title = result
        if not songs:
            return await interaction.edit_original_response(content="❌ Playlist appears to be empty!")
        was_empty = len(queue["songs"]) == 0
        for song in songs:
            song["requester"] = interaction.user.display_name
            song["requester_id"] = interaction.user.id
            queue["songs"].append(song)
        embed = discord.Embed(
            title="📋 Playlist Added",
            description=f"**{playlist_title}**\nAdded **{len(songs)} songs** to the queue!",
            color=0x1DB954,
        )
        embed.set_footer(text=f"Queued by {interaction.user.display_name}")
        await interaction.edit_original_response(content=None, embed=embed)
        if was_empty:
            await play_next(interaction.guild, interaction.channel)
    else:
        song = await search_yt(query)
        if not song:
            return await interaction.edit_original_response(content="❌ Could not find that song. Try a different search!")
        song["requester"] = interaction.user.display_name
        song["requester_id"] = interaction.user.id
        queue["songs"].append(song)
        if len(queue["songs"]) == 1:
            await interaction.edit_original_response(content="✅ Found it! Starting playback...")
            await play_next(interaction.guild, interaction.channel)
        else:
            await interaction.edit_original_response(content=f"✅ Added to queue: **{song['title']}** (position #{len(queue['songs'])})")



@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    vc = queue.get("voice_client")
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
    if not can_control_interaction(interaction, queue):
        return await interaction.response.send_message("❌ Only the person who queued this song can skip it!", ephemeral=True)
    vc.stop()
    await interaction.response.send_message("⏭️ Skipped!")

@bot.tree.command(name="stop", description="Stop music and clear the queue")
async def stop(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    vc = queue.get("voice_client")
    if not vc:
        return await interaction.response.send_message("❌ Not connected to a voice channel!", ephemeral=True)
    if not can_control_interaction(interaction, queue):
        return await interaction.response.send_message("❌ Only the person who queued this song can stop the music!", ephemeral=True)
    queue["songs"] = []
    vc.stop()
    await vc.disconnect()
    queues.pop(interaction.guild.id, None)
    await interaction.response.send_message("⏹️ Stopped and cleared the queue.")

@bot.tree.command(name="pause", description="Pause the current song")
async def pause(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    vc = queue.get("voice_client")
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
    if not can_control_interaction(interaction, queue):
        return await interaction.response.send_message("❌ Only the person who queued this song can pause it!", ephemeral=True)
    vc.pause()
    await interaction.response.send_message("⏸️ Paused.")

@bot.tree.command(name="resume", description="Resume the paused song")
async def resume(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    vc = queue.get("voice_client")
    if not vc or not vc.is_paused():
        return await interaction.response.send_message("❌ Nothing is paused!", ephemeral=True)
    if not can_control_interaction(interaction, queue):
        return await interaction.response.send_message("❌ Only the person who queued this song can resume it!", ephemeral=True)
    vc.resume()
    await interaction.response.send_message("▶️ Resumed!")

@bot.tree.command(name="queue", description="Show the current music queue")
async def show_queue(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    if not queue["songs"]:
        return await interaction.response.send_message("📭 The queue is empty!")
    lines = []
    for i, song in enumerate(queue["songs"]):
        prefix = "▶️" if i == 0 else f"{i}."
        lines.append(f"{prefix} **{song['title']}** — {song['duration']}")
    description = "\n".join(lines)
    if len(description) > 2000:
        description = description[:2000] + "..."
    embed = discord.Embed(title="🎵 Music Queue", description=description, color=0x1DB954)
    embed.set_footer(text=f"{len(queue['songs'])} song(s)")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="np", description="Show the currently playing song")
async def np(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    if not queue["songs"]:
        return await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
    song = queue["songs"][0]
    embed = discord.Embed(title="🎵 Now Playing", description=f"**[{song['title']}]({song['url']})**", color=0x1DB954)
    embed.add_field(name="Duration", value=song["duration"], inline=True)
    if song.get("thumbnail"):
        embed.set_thumbnail(url=song["thumbnail"])
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="volume", description="Set the music volume")
@app_commands.describe(level="Volume level from 1 to 100")
async def volume(interaction: discord.Interaction, level: int):
    queue = get_queue(interaction.guild.id)
    if not (1 <= level <= 100):
        return await interaction.response.send_message("❌ Volume must be between 1 and 100!", ephemeral=True)
    queue["volume"] = level / 100
    vc = queue.get("voice_client")
    if vc and vc.source:
        vc.source.volume = queue["volume"]
    await interaction.response.send_message(f"🔊 Volume set to **{level}%**")


async def restart_with_effect(interaction: discord.Interaction, queue: dict, vc, label: str):
    """Re-fetches the stream and restarts playback with current queue effects."""
    song = queue["songs"][0]
    queue["restarting"] = True
    vc.stop()
    await asyncio.sleep(0.8)
    try:
        fresh = await search_yt(song["url"])
        if not fresh:
            raise Exception("Could not re-fetch song")
        song["stream_url"] = fresh["stream_url"]
    except Exception as e:
        print(f"Re-fetch error: {e}")
        queue["restarting"] = False
        await interaction.channel.send("❌ Could not reload the song. Try `/play` again.")
        return
    try:
        ffmpeg_opts = build_ffmpeg_options(queue["bass_boost"], queue.get("effect", "none"))
        source = discord.FFmpegPCMAudio(song["stream_url"], **ffmpeg_opts)
        source = discord.PCMVolumeTransformer(source, volume=queue["volume"])

        def after_play(error):
            if error:
                print(f"Player error: {error}")
            if queue.get("restarting"):
                return
            if queue["songs"]:
                queue["songs"].pop(0)
            asyncio.run_coroutine_threadsafe(play_next(interaction.guild, interaction.channel), bot.loop)

        queue["restarting"] = False
        vc.play(source, after=after_play)
        embed = discord.Embed(
            title=f"🎵 Now Playing — {label}",
            description=f"**[{song['title']}]({song['url']})**",
            color=0x1DB954,
        )
        embed.add_field(name="Duration", value=song["duration"], inline=True)
        if song.get("thumbnail"):
            embed.set_thumbnail(url=song["thumbnail"])
        await interaction.channel.send(embed=embed)
    except Exception as e:
        print(f"Effect restart error: {e}")
        queue["restarting"] = False
        await interaction.channel.send("❌ Error restarting song.")

@bot.tree.command(name="bassboost", description="Set bass boost level (0 = off, 1-5 = mild, 6-15 = strong, 16-30 = extreme)")
@app_commands.describe(level="Bass boost level: 0 to turn off, 1-10 normal, 11-30 extreme")
async def bassboost(interaction: discord.Interaction, level: int):
    if not 0 <= level <= 30:
        return await interaction.response.send_message("❌ Level must be between 0 and 30.", ephemeral=True)
    queue = get_queue(interaction.guild.id)
    vc = queue.get("voice_client")
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
    if not queue["songs"]:
        return await interaction.response.send_message("❌ Nothing in the queue!", ephemeral=True)
    queue["bass_boost"] = level
    label = f"🎸 Bass Boost +{level}dB" if level > 0 else "No Effects"
    await interaction.response.send_message(f"🎸 Bass boost set to **+{level}dB** — restarting..." if level > 0 else "🔈 Bass boost **OFF** — restarting...")
    await restart_with_effect(interaction, queue, vc, label)


# ─── Audio Effects ────────────────────────────────────────────────────────────
@bot.tree.command(name="effect", description="Apply an audio effect to the current song")
@app_commands.describe(name="The effect to apply")
@app_commands.choices(name=[
    app_commands.Choice(name="None (remove all effects)", value="none"),
    app_commands.Choice(name="8D Audio 🎧", value="8d"),
    app_commands.Choice(name="Nightcore ⚡", value="nightcore"),
    app_commands.Choice(name="Slowed 🐢", value="slowed"),
    app_commands.Choice(name="Vaporwave 🌊", value="vaporwave"),
    app_commands.Choice(name="Echo 🔁", value="echo"),
    app_commands.Choice(name="Reverb 🏛️", value="reverb"),
    app_commands.Choice(name="Earrape 💀", value="earrape"),
    app_commands.Choice(name="Underwater 🌊", value="underwater"),
    app_commands.Choice(name="Robot 🤖", value="robot"),
])
async def effect(interaction: discord.Interaction, name: str):
    queue = get_queue(interaction.guild.id)
    vc = queue.get("voice_client")
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
    if not queue["songs"]:
        return await interaction.response.send_message("❌ Nothing in the queue!", ephemeral=True)
    queue["effect"] = name
    labels = {
        "none": "No Effects", "8d": "8D Audio 🎧", "nightcore": "Nightcore ⚡",
        "slowed": "Slowed 🐢", "vaporwave": "Vaporwave 🌊", "echo": "Echo 🔁",
        "reverb": "Reverb 🏛️", "earrape": "Earrape 💀", "underwater": "Underwater 🌊",
        "robot": "Robot 🤖",
    }
    label = labels.get(name, name)
    await interaction.response.send_message(f"✨ Applying **{label}** — restarting song...")
    await restart_with_effect(interaction, queue, vc, label)

@bot.tree.command(name="lyrics", description="Show lyrics for a song (uses current song if left blank)")
@app_commands.describe(query="Song to search — use 'Artist - Title' format for best results")
async def lyrics(interaction: discord.Interaction, query: str = None):
    if not query:
        queue = get_queue(interaction.guild.id)
        if not queue["songs"]:
            return await interaction.response.send_message("❌ Nothing is playing! Provide a song name.", ephemeral=True)
        query = queue["songs"][0]["title"]

    await interaction.response.send_message(f"🔍 Searching lyrics for **{query}**...")

    try:
        clean = re.sub(r"[\(\[].*?[\)\]]", "", query).strip()
        parts = clean.split(" - ", 1)
        if len(parts) == 2:
            artist, title = parts[0].strip(), parts[1].strip()
        else:
            artist, title = "", clean

        if artist:
            url = f"https://api.lyrics.ovh/v1/{urllib.parse.quote(artist)}/{urllib.parse.quote(title)}"
        else:
            suggest_url = f"https://api.lyrics.ovh/suggest/{urllib.parse.quote(clean)}"
            req = urllib.request.Request(suggest_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            if not data.get("data"):
                return await interaction.edit_original_response(content=f"❌ Couldn't find lyrics for **{query}**. Try `/lyrics Artist - Song Title`")
            top = data["data"][0]
            artist = top["artist"]["name"]
            title = top["title"]
            url = f"https://api.lyrics.ovh/v1/{urllib.parse.quote(artist)}/{urllib.parse.quote(title)}"

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())

        lyrics_text = data.get("lyrics", "").strip()
        if not lyrics_text:
            return await interaction.edit_original_response(content=f"❌ No lyrics found for **{query}**.")

        chunks = []
        while len(lyrics_text) > 1900:
            split_at = lyrics_text.rfind("\n", 0, 1900)
            if split_at == -1:
                split_at = 1900
            chunks.append(lyrics_text[:split_at])
            lyrics_text = lyrics_text[split_at:].lstrip("\n")
        chunks.append(lyrics_text)

        await interaction.edit_original_response(content="✅ Found it!")
        for i, chunk in enumerate(chunks):
            embed = discord.Embed(
                title=f"🎤 {title} — {artist}" if i == 0 else f"🎤 {title} — {artist} (cont.)",
                description=chunk,
                color=0xEB459E,
            )
            if i == len(chunks) - 1:
                embed.set_footer(text="Lyrics via lyrics.ovh")
            await interaction.channel.send(embed=embed)

    except Exception as e:
        await interaction.edit_original_response(content=f"❌ Couldn't find lyrics for **{query}**. Try `/lyrics Artist - Song Title`")
        print(f"Lyrics error: {e}")


# ─── Give Coins (Owner Only) ──────────────────────────────────────────────────
OWNER_ID = 1349855656408121404

@bot.tree.command(name="givecoins", description="Give coins to a user (owner only)")
@app_commands.describe(member="Who to give coins to", amount="How many coins to give")
async def givecoins(interaction: discord.Interaction, member: discord.Member, amount: int):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("❌ Only the bot owner can give coins.", ephemeral=True)
    if amount <= 0:
        return await interaction.response.send_message("❌ Amount must be at least 1.", ephemeral=True)
    add_points(member.id, amount)
    await interaction.response.send_message(f"🪙 Gave **{amount} coins** to {member.mention}! They now have **{get_points(member.id)} coins**.")

@bot.tree.command(name="removecoins", description="Remove coins from a user (owner only)")
@app_commands.describe(member="Who to remove coins from", amount="How many coins to remove")
async def removecoins(interaction: discord.Interaction, member: discord.Member, amount: int):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("❌ Only the bot owner can remove coins.", ephemeral=True)
    if amount <= 0:
        return await interaction.response.send_message("❌ Amount must be at least 1.", ephemeral=True)
    remove_points(member.id, amount)
    await interaction.response.send_message(f"🪙 Removed **{amount} coins** from {member.mention}. They now have **{get_points(member.id)} coins**.")

# ─── Run ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(TOKEN)
