import discord
from discord.ext import commands
from discord import app_commands
import utils
import asyncio
import datetime
from typing import Optional

class Social(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def get_ai_cog(self):
        return self.bot.get_cog("AI")

    @app_commands.command(name="roast", description="DESTROY someone based on history.")
    async def roast(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()
        dossier = utils.get_user_dossier(member)
        history = await utils.get_user_history_text(self.bot.chat_collection, member.id)
        pfp = await utils.get_image_from_url(member.display_avatar.url) if member.display_avatar else None
        
        prompt = (f"TARGET:\n{dossier}\nRECENT CHATS:\n{history}\n"
                  f"INSTRUCTION: Roast them based on PFP and chat history. Call them out on things they said. Be brutal.")
        
        ai = await self.get_ai_cog()
        resp, _ = await ai.get_combined_response(interaction.user.id, None, image_input=pfp, prompt_override=prompt)
        await utils.send_chunked_reply(interaction, f"{member.mention} {resp}")

    @app_commands.command(name="rate", description="Judge vibe based on chat history.")
    async def rate(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()
        dossier = utils.get_user_dossier(member)
        history = await utils.get_user_history_text(self.bot.chat_collection, member.id)
        pfp = await utils.get_image_from_url(member.display_avatar.url) if member.display_avatar else None
        
        prompt = (f"TARGET:\n{dossier}\nRECENT CHATS:\n{history}\n"
                  f"INSTRUCTION: Rate vibe (0-100%). If they are funny/nice in chats, give high score. If dry/rude, destroy them.")
        
        ai = await self.get_ai_cog()
        resp, _ = await ai.get_combined_response(interaction.user.id, None, image_input=pfp, prompt_override=prompt)
        await utils.send_chunked_reply(interaction, f"{member.mention} {resp}")

    @app_commands.command(name="ship", description="Check compatibility.")
    async def ship(self, interaction: discord.Interaction, member1: discord.Member, member2: Optional[discord.Member] = None):
        await interaction.response.defer()
        target2 = member2 if member2 else interaction.user

        if member1.id == target2.id:
            await interaction.followup.send("shipping yourself?? bro please 💀")
            return

        d1 = utils.get_user_dossier(member1)
        d2 = utils.get_user_dossier(target2)
        h1 = await utils.get_user_history_text(self.bot.chat_collection, member1.id, limit=30)
        h2 = await utils.get_user_history_text(self.bot.chat_collection, target2.id, limit=30)

        # Fetch actual channel interactions between the two
        interactions_found = []
        async for msg in interaction.channel.history(limit=100):
            if msg.author.bot:
                continue
            if msg.author.id in (member1.id, target2.id):
                safe_content = utils.sanitize_for_prompt(msg.content)
                if safe_content.strip():
                    interactions_found.append(f"{msg.author.display_name}: {safe_content}")
            if len(interactions_found) >= 20:
                break

        interactions_found.reverse()
        interaction_log = "\n".join(interactions_found) if interactions_found else "No recent interactions found in this channel."

        combined_img = None
        if member1.display_avatar and target2.display_avatar:
            img1 = await utils.get_image_from_url(member1.display_avatar.url)
            img2 = await utils.get_image_from_url(target2.display_avatar.url)
            if img1 and img2:
                combined_img = await asyncio.to_thread(utils.stitch_images, img1, img2)

        prompt = (
            f"Ship these two people based on their ACTUAL messages and interactions.\n\n"
            f"PERSON 1 — {member1.display_name}:\n{d1}\nTheir messages:\n{h1}\n\n"
            f"PERSON 2 — {target2.display_name}:\n{d2}\nTheir messages:\n{h2}\n\n"
            f"Their recent channel interactions:\n{interaction_log}\n\n"
            f"INSTRUCTION: Give a ship name, a % score, and judge if they'd actually work. "
            f"Reference specific things from their messages. Be dramatic and chaotic as Yuri."
        )

        ai = await self.get_ai_cog()
        resp, _ = await ai.get_combined_response(interaction.user.id, None, image_input=combined_img, prompt_override=prompt)

        embed = discord.Embed(
            title=f"💘 {member1.display_name} × {target2.display_name}",
            description=resp,
            color=discord.Color.from_rgb(255, 105, 180)
        )
        embed.set_footer(text="based on actual message history and interactions")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="confess", description="Send an anonymous confession.")
    async def confess(self, interaction: discord.Interaction, message: str):
        await interaction.response.defer(ephemeral=True)
        config = await self.bot.config_collection.find_one({"guild_id": interaction.guild_id})
        
        if not config or "confession_channel_id" not in config:
            await interaction.followup.send("❌ Admin must run `/setup` first!", ephemeral=True)
            return
            
        channel = interaction.guild.get_channel(config["confession_channel_id"]) or await interaction.guild.fetch_channel(config["confession_channel_id"])
        
        embed = discord.Embed(title="📨 Anonymous Confession", description=f'"{message}"', color=discord.Color.random())
        embed.set_footer(text="Sent via /confess • Identity Hidden")
        await channel.send(embed=embed)
        await interaction.followup.send("✅ Sent!", ephemeral=True)

    @app_commands.command(name="crush", description="Secretly match with your crush!")
    async def crush(self, interaction: discord.Interaction, target: discord.Member):
        await interaction.response.defer(ephemeral=True)
        if target.id == interaction.user.id or target.bot:
            await interaction.followup.send("Invalid target lol. 💀", ephemeral=True)
            return
            
        match = await self.bot.crush_collection.find_one({"lover_id": target.id, "target_id": interaction.user.id})
        if match:
            try: await interaction.user.send(f"💖 **MATCH!** {target.display_name} likes you back!")
            except: pass
            try: await target.send(f"💖 **MATCH!** {interaction.user.display_name} likes you back!")
            except: pass
            await interaction.channel.send(f"@everyone 🚨 **LOVE ALERT:** Two people just matched via `/crush`! 💍✨")
            await self.bot.crush_collection.delete_one({"_id": match["_id"]})
            await interaction.followup.send("💖 **IT'S A MATCH!**", ephemeral=True)
        else:
            await self.bot.crush_collection.update_one(
                {"lover_id": interaction.user.id, "target_id": target.id},
                {"$set": {"timestamp": datetime.datetime.utcnow()}},
                upsert=True
            )
            await interaction.followup.send(f"🤫 **Secret Kept.**", ephemeral=True)

    @app_commands.command(name="truth", description="Get a spicy Truth question.")
    async def truth(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ai = await self.get_ai_cog()
        resp, _ = await ai.get_combined_response(interaction.user.id, None, prompt_override="Give a funny, spicy teenage Truth question.")
        await utils.send_chunked_reply(interaction, f"**TRUTH:** {resp}")

    @app_commands.command(name="dare", description="Get a chaotic Dare.")
    async def dare(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ai = await self.get_ai_cog()
        resp, _ = await ai.get_combined_response(interaction.user.id, None, prompt_override="Give a funny, chaotic Dare for a discord user.")
        await utils.send_chunked_reply(interaction, f"**DARE:** {resp}")

    @app_commands.command(name="poll", description="Yuri hosts a drama-style poll and picks a side.")
    async def poll(self, interaction: discord.Interaction, question: str, option1: str, option2: str):
        await interaction.response.defer()

        safe_q = utils.sanitize_for_prompt(question)
        safe_o1 = utils.sanitize_for_prompt(option1)
        safe_o2 = utils.sanitize_for_prompt(option2)

        ai = await self.get_ai_cog()
        prompt = (
            f"There's a poll: '{safe_q}' with options '{safe_o1}' vs '{safe_o2}'. "
            f"Pick a side strongly and give a short spicy opinion on why your choice wins. Be dramatic and chaotic."
        )
        yuri_take, _ = await ai.get_combined_response(interaction.user.id, None, prompt_override=prompt)

        embed = discord.Embed(
            title=f"🗳️ {question}",
            description=f"Yuri's take: {yuri_take}",
            color=discord.Color.from_rgb(255, 105, 180)
        )
        embed.add_field(name="🅰️ Option 1", value=option1, inline=True)
        embed.add_field(name="🅱️ Option 2", value=option2, inline=True)
        embed.set_footer(text="React below to vote!")

        poll_msg = await interaction.followup.send(embed=embed)
        await poll_msg.add_reaction("🅰️")
        await poll_msg.add_reaction("🅱️")

    @app_commands.command(name="hotornot", description="Submit an anonymous description and let the server judge you.")
    async def hotornot(self, interaction: discord.Interaction, description: str):
        await interaction.response.defer(ephemeral=True)

        config = await self.bot.config_collection.find_one({"guild_id": interaction.guild_id})
        channel_id = config.get("confession_channel_id") if config else None
        channel = interaction.channel

        if channel_id:
            fetched = interaction.guild.get_channel(channel_id) or await interaction.guild.fetch_channel(channel_id)
            if fetched:
                channel = fetched

        safe_desc = utils.sanitize_for_prompt(description)
        ai = await self.get_ai_cog()
        prompt = (
            f"Someone anonymously submitted this for a hot or not judgment: '{safe_desc}'. "
            f"Write a short dramatic intro for the server to read before they vote. Be chaotic and hype it up."
        )
        yuri_intro, _ = await ai.get_combined_response(interaction.user.id, None, prompt_override=prompt)

        embed = discord.Embed(
            title="🚨 ANONYMOUS SUBMISSION 🚨",
            description=f"**Case:**\n*\"{description}\"*\n\n**Yuri says:** {yuri_intro}",
            color=discord.Color.from_rgb(255, 105, 180)
        )
        embed.set_footer(text="🔥 = Hot   💀 = Not  |  Verdict in 15 minutes")

        vote_msg = await channel.send(embed=embed)
        await vote_msg.add_reaction("🔥")
        await vote_msg.add_reaction("💀")
        await interaction.followup.send("✅ submitted anonymously. good luck bestie 💀", ephemeral=True)

        await asyncio.sleep(900)  # 15 minutes

        vote_msg = await channel.fetch_message(vote_msg.id)
        hot_count = 0
        not_count = 0
        for reaction in vote_msg.reactions:
            if str(reaction.emoji) == "🔥":
                hot_count = reaction.count - 1  # subtract bot's own reaction
            elif str(reaction.emoji) == "💀":
                not_count = reaction.count - 1

        verdict_prompt = (
            f"A hotornot vote just ended. Someone described themselves as: '{safe_desc}'. "
            f"Results: {hot_count} voted 🔥 hot, {not_count} voted 💀 not. "
            f"Deliver the verdict dramatically as Yuri. Be funny and ruthless."
        )
        verdict, _ = await ai.get_combined_response(interaction.user.id, None, prompt_override=verdict_prompt)

        result_embed = discord.Embed(
            title="⚖️ THE VERDICT IS IN",
            description=f"🔥 **{hot_count}** vs 💀 **{not_count}**\n\n{verdict}",
            color=discord.Color.green() if hot_count >= not_count else discord.Color.red()
        )
        await channel.send(embed=result_embed)

    @app_commands.command(name="summarize", description="Yuri summarizes the last 20 messages in this channel.")
    async def summarize(self, interaction: discord.Interaction):
        await interaction.response.defer()

        # Fetch last 20 messages from the channel (excluding bot messages)
        messages = []
        async for msg in interaction.channel.history(limit=30):
            if msg.author.bot:
                continue
            if len(messages) >= 20:
                break
            safe_content = utils.sanitize_for_prompt(msg.content)
            if safe_content.strip():
                messages.append(f"{msg.author.display_name}: {safe_content}")

        if not messages:
            await interaction.followup.send("there's literally nothing to summarize bro 💀")
            return

        # Reverse so it's chronological
        messages.reverse()
        chat_log = "\n".join(messages)

        ai = await self.get_ai_cog()
        prompt = (
            f"Here are the last 20 messages in a Discord server:\n\n{chat_log}\n\n"
            f"Summarize what's going on in this chat as Yuri. Be chaotic, dramatic, and gen z. "
            f"Point out any drama, funny moments, or weird vibes. Keep it short."
        )
        resp, _ = await ai.get_combined_response(interaction.user.id, None, prompt_override=prompt)
        
        embed = discord.Embed(
            title="📋 CHAT SUMMARY",
            description=resp,
            color=discord.Color.from_rgb(255, 105, 180)
        )
        embed.set_footer(text="based on the last 20 messages")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="compatibility", description="Deep compatibility check based on actual server messages.")
    async def compatibility(self, interaction: discord.Interaction, member1: discord.Member, member2: Optional[discord.Member] = None):
        await interaction.response.defer()

        target2 = member2 if member2 else interaction.user

        if member1.id == target2.id:
            await interaction.followup.send("bro is trying to ship themselves 💀 seek help")
            return

        # Get dossiers
        d1 = utils.get_user_dossier(member1)
        d2 = utils.get_user_dossier(target2)

        # Get deep chat history (more messages for better analysis)
        h1 = await utils.get_user_history_text(self.bot.chat_collection, member1.id, limit=30)
        h2 = await utils.get_user_history_text(self.bot.chat_collection, target2.id, limit=30)

        # Also fetch recent channel messages to see if they've interacted
        interactions_found = []
        async for msg in interaction.channel.history(limit=100):
            if msg.author.bot:
                continue
            if msg.author.id in (member1.id, target2.id):
                safe_content = utils.sanitize_for_prompt(msg.content)
                if safe_content.strip():
                    interactions_found.append(f"{msg.author.display_name}: {safe_content}")
            if len(interactions_found) >= 20:
                break

        interactions_found.reverse()
        interaction_log = "\n".join(interactions_found) if interactions_found else "No recent interactions found in this channel."

        ai = await self.get_ai_cog()
        prompt = (
            f"Do a DEEP compatibility analysis between two people.\n\n"
            f"PERSON 1 — {member1.display_name}:\n{d1}\nTheir messages with Yuri:\n{h1}\n\n"
            f"PERSON 2 — {target2.display_name}:\n{d2}\nTheir messages with Yuri:\n{h2}\n\n"
            f"Their recent channel interactions:\n{interaction_log}\n\n"
            f"INSTRUCTION: Analyze their communication styles, energy levels, humor, and any actual interactions. "
            f"Give a compatibility % score and explain WHY they work or don't work. "
            f"Call out specific things from their messages. Be dramatic, specific, and chaotic as Yuri. "
            f"Format it like a proper compatibility report but in gen z language."
        )

        resp, _ = await ai.get_combined_response(interaction.user.id, None, prompt_override=prompt)

        embed = discord.Embed(
            title=f"💘 {member1.display_name} × {target2.display_name}",
            description=resp,
            color=discord.Color.from_rgb(255, 105, 180)
        )
        embed.set_footer(text="based on actual message history and interactions")
        await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Social(bot))
        
