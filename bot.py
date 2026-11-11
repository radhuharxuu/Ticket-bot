import io
import os
import asyncio
import datetime
import aiohttp
from aiohttp import web
import discord
from discord.ext import commands
from discord import app_commands

# ======================= ENVIRONMENT CONFIGURATION =======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:8080/callback")
PORT = int(os.getenv("PORT", "8080"))

SUPPORT_ROLE_ID = int(os.getenv("SUPPORT_ROLE_ID", "0"))
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "1443810072265363518"))
# =========================================================================


class TicketBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(TicketLauncherView())
        self.add_view(TicketControlView())
        await self.tree.sync()
        # Launch web server inside the bot's event loop
        self.loop.create_task(start_web_server())


bot = TicketBot()


# -------------------- TICKET ENGINE --------------------
async def generate_transcript(channel: discord.TextChannel) -> discord.File:
    lines = [
        f"=== TRANSCRIPT: #{channel.name} ===",
        f"Generated At: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "=" * 45, "\n"
    ]
    async for msg in channel.history(limit=1000, oldest_first=True):
        timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        content = msg.clean_content or "[No text]"
        if msg.attachments:
            content += f" [Files: {', '.join(a.url for a in msg.attachments)}]"
        lines.append(f"[{timestamp}] {msg.author.name} ({msg.author.id}):\n{content}\n")

    return discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"transcript-{channel.name}.txt")


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Transcript", style=discord.ButtonStyle.secondary, emoji="📜", custom_id="ticket_transcript_btn")
    async def transcript_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        file = await generate_transcript(interaction.channel)
        await interaction.followup.send("Ticket transcript:", file=file, ephemeral=True)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket_close_btn")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Closing ticket in 5 seconds...")
        channel = interaction.channel
        log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            file = await generate_transcript(channel)
            embed = discord.Embed(title="Ticket Closed", color=discord.Color.red())
            embed.add_field(name="Ticket", value=channel.name)
            embed.add_field(name="Closed By", value=f"{interaction.user.mention}")
            await log_channel.send(embed=embed, file=file)
        await asyncio.sleep(5)
        await channel.delete()


class TicketLauncherView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.primary, emoji="📩", custom_id="ticket_launcher_create_btn")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user
        sanitized_name = f"ticket-{user.name.lower().replace(' ', '-')}"

        if discord.utils.get(guild.text_channels, name=sanitized_name):
            await interaction.response.send_message("You already have an open ticket.", ephemeral=True)
            return

        category = guild.get_channel(TICKET_CATEGORY_ID)
        support_role = guild.get_role(SUPPORT_ROLE_ID)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
        }
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        ticket_channel = await guild.create_text_channel(name=sanitized_name, category=category, overwrites=overwrites)
        embed = discord.Embed(title="Support Ticket", description=f"Welcome {user.mention}. Support will be here shortly.", color=discord.Color.green())
        await ticket_channel.send(content=f"{user.mention} {support_role.mention if support_role else ''}", embed=embed, view=TicketControlView())
        await interaction.response.send_message(f"Created: {ticket_channel.mention}", ephemeral=True)


# -------------------- SLASH COMMANDS --------------------
@bot.tree.command(name="setup_panel", description="Deploy the ticket panel.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_panel(interaction: discord.Interaction):
    embed = discord.Embed(title="Support Hub", description="Click below to open a ticket.", color=discord.Color.blurple())
    await interaction.channel.send(embed=embed, view=TicketLauncherView())
    await interaction.response.send_message("Panel deployed!", ephemeral=True)


@bot.tree.command(name="setav", description="Change bot avatar.")
async def setav(interaction: discord.Interaction, image: discord.Attachment):
    if interaction.user.id != AUTHORIZED_USER_ID:
        return await interaction.response.send_message("❌ Unauthorized.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await bot.user.edit(avatar=await image.read())
    await interaction.followup.send("✅ Avatar updated!", ephemeral=True)


@bot.tree.command(name="setbner", description="Change bot banner.")
async def setbner(interaction: discord.Interaction, image: discord.Attachment):
    if interaction.user.id != AUTHORIZED_USER_ID:
        return await interaction.response.send_message("❌ Unauthorized.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await bot.user.edit(banner=await image.read())
    await interaction.followup.send("✅ Banner updated!", ephemeral=True)


# -------------------- WEB SERVER & DASHBOARD --------------------
routes = web.RouteTableDef()

# In-memory session store (simple token -> user data map)
SESSIONS = {}

LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Ticket Bot Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>body { background: #0f172a; color: #f8fafc; font-family: sans-serif; }</style>
</head>
<body class="d-flex align-items-center justify-content-center vh-100">
    <div class="card bg-dark text-white p-4 shadow" style="max-width: 400px; width: 100%;">
        <h3 class="text-center mb-3">Bot Control Hub</h3>
        <p class="text-secondary text-center">Manage tickets and customize your bot from the web.</p>
        <a href="/login" class="btn btn-primary w-100 py-2">Login with Discord</a>
    </div>
</body>
</html>
"""

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>body { background: #0f172a; color: #f8fafc; font-family: sans-serif; }</style>
</head>
<body class="p-4">
    <div class="container" style="max-width: 700px;">
        <div class="d-flex justify-content-between align-items-center mb-4 pb-2 border-bottom border-secondary">
            <h2>Welcome, {username}</h2>
            <span class="badge {role_badge}">{role_name}</span>
        </div>

        {owner_section}

        <div class="card bg-dark text-white p-4 mb-4">
            <h4>Deploy Support Panel</h4>
            <p class="text-secondary">Send a ticket panel directly into any Discord text channel.</p>
            <form action="/web-deploy-panel" method="POST">
                <input type="hidden" name="session_token" value="{token}">
                <div class="mb-3">
                    <label class="form-label">Channel ID</label>
                    <input type="text" name="channel_id" class="form-control bg-secondary text-white" placeholder="e.g. 123456789012345678" required>
                </div>
                <button type="submit" class="btn btn-success">Deploy Panel</button>
            </form>
        </div>
    </div>
</body>
</html>
"""

OWNER_SECTION_HTML = """
<div class="card bg-dark text-white p-4 mb-4 border-warning border">
    <h4 class="text-warning">Owner Controls (ID: 1443810072265363518)</h4>
    <p class="text-secondary">Change bot visuals directly from your browser.</p>

    <form action="/web-setav" method="POST" enctype="multipart/form-data" class="mb-3">
        <input type="hidden" name="session_token" value="{token}">
        <label class="form-label">Change Bot Avatar</label>
        <div class="input-group">
            <input type="file" name="avatar" class="form-control bg-secondary text-white" accept="image/*" required>
            <button class="btn btn-outline-warning" type="submit">Update Avatar</button>
        </div>
    </form>

    <form action="/web-setbner" method="POST" enctype="multipart/form-data">
        <input type="hidden" name="session_token" value="{token}">
        <label class="form-label">Change Bot Banner</label>
        <div class="input-group">
            <input type="file" name="banner" class="form-control bg-secondary text-white" accept="image/*" required>
            <button class="btn btn-outline-warning" type="submit">Update Banner</button>
        </div>
    </form>
</div>
"""


@routes.get("/")
async def index(request):
    token = request.cookies.get("session_token")
    if token and token in SESSIONS:
        user = SESSIONS[token]
        is_owner = (int(user["id"]) == AUTHORIZED_USER_ID)
        owner_html = OWNER_SECTION_HTML.format(token=token) if is_owner else ""

        html = DASHBOARD_HTML.format(
            username=user["username"],
            role_name="Bot Owner" if is_owner else "Administrator",
            role_badge="bg-warning text-dark" if is_owner else "bg-info text-dark",
            owner_section=owner_html,
            token=token
        )
        return web.Response(text=html, content_type="text/html")
    return web.Response(text=LOGIN_HTML, content_type="text/html")


@routes.get("/login")
async def oauth_login(request):
    client_id = bot.user.id if bot.user else "YOUR_CLIENT_ID"
    discord_login_url = (
        f"https://discord.com/api/oauth2/authorize?client_id={client_id}"
        f"&redirect_uri={REDIRECT_URI}&response_type=code&scope=identify"
    )
    raise web.HTTPFound(discord_login_url)


@routes.get("/callback")
async def oauth_callback(request):
    code = request.query.get("code")
    if not code:
        return web.Response(text="Authorization failed: No code returned.", status=400)

    # Exchange authorization code for token
    async with aiohttp.ClientSession() as session:
        data = {
            "client_id": str(bot.user.id),
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        async with session.post("https://discord.com/api/oauth2/token", data=data, headers=headers) as resp:
            token_data = await resp.json()
            access_token = token_data.get("access_token")

        if not access_token:
            return web.Response(text="Failed to obtain access token.", status=400)

        # Fetch authenticated user profile
        auth_headers = {"Authorization": f"Bearer {access_token}"}
        async with session.get("https://discord.com/api/users/@me", headers=auth_headers) as user_resp:
            user_info = await user_resp.json()

    # Save session
    session_token = os.urandom(24).hex()
    SESSIONS[session_token] = user_info

    response = web.HTTPFound("/")
    response.set_cookie("session_token", session_token, max_age=86400)
    return response


@routes.post("/web-setav")
async def web_setav(request):
    reader = await request.multipart()
    token = None
    file_bytes = None

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "session_token":
            token = (await part.read()).decode("utf-8")
        elif part.name == "avatar":
            file_bytes = await part.read()

    user = SESSIONS.get(token)
    if not user or int(user["id"]) != AUTHORIZED_USER_ID:
        return web.Response(text="403 Forbidden: You are not authorized.", status=403)

    if file_bytes:
        await bot.user.edit(avatar=file_bytes)
        return web.Response(text="<h1>✅ Avatar updated!</h1><a href='/'>Return to dashboard</a>", content_type="text/html")
    return web.Response(text="No file uploaded.", status=400)


@routes.post("/web-setbner")
async def web_setbner(request):
    reader = await request.multipart()
    token = None
    file_bytes = None

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "session_token":
            token = (await part.read()).decode("utf-8")
        elif part.name == "banner":
            file_bytes = await part.read()

    user = SESSIONS.get(token)
    if not user or int(user["id"]) != AUTHORIZED_USER_ID:
        return web.Response(text="403 Forbidden: You are not authorized.", status=403)

    if file_bytes:
        await bot.user.edit(banner=file_bytes)
        return web.Response(text="<h1>✅ Banner updated!</h1><a href='/'>Return to dashboard</a>", content_type="text/html")
    return web.Response(text="No file uploaded.", status=400)


@routes.post("/web-deploy-panel")
async def web_deploy_panel(request):
    data = await request.post()
    token = data.get("session_token")
    channel_id_raw = data.get("channel_id")

    user = SESSIONS.get(token)
    if not user:
        return web.Response(text="Unauthorized", status=401)

    try:
        channel_id = int(channel_id_raw)
        channel = bot.get_channel(channel_id)
        if not channel:
            return web.Response(text="Error: Bot cannot find that channel.", status=404)

        embed = discord.Embed(
            title="Support Hub",
            description="Click the button below to open a ticket.",
            color=discord.Color.blurple()
        )
        await channel.send(embed=embed, view=TicketLauncherView())
        return web.Response(text="<h1>✅ Panel deployed!</h1><a href='/'>Return to dashboard</a>", content_type="text/html")
    except ValueError:
        return web.Response(text="Invalid channel ID.", status=400)


async def start_web_server():
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Web dashboard running on http://0.0.0.0:{PORT}")


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing!")
    bot.run(BOT_TOKEN)
