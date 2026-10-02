from .ytaudio import YTAudio


async def setup(bot):
    await bot.add_cog(YTAudio(bot))
