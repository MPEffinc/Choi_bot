"""Interaction-aware output; generation/retry remains outside this module."""
import logging

logger = logging.getLogger(__name__)


def _progress_failure(operation, error):
    # Useful lifecycle diagnostics without exception bodies, webhook URLs or tokens.
    logger.warning('Discord progress %s failed: type=%s status=%s code=%s',
                   operation, type(error).__name__, getattr(error, 'status', None),
                   getattr(error, 'code', None))


def split_text(content, limit=2000):
    # Count UTF-16 units conservatively (emoji can occupy two units in Discord).
    content = str(content)
    chunks, current, size = [], [], 0
    for char in content:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            chunks.append(''.join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current or not chunks:
        chunks.append(''.join(current))
    return chunks


async def send(interaction, content, *, ephemeral=False, view=None):
    result = None
    for index, chunk in enumerate(split_text(content)):
        kwargs = {'ephemeral': ephemeral}
        if view is not None and index == 0:
            kwargs['view'] = view
        if not interaction.response.is_done():
            await interaction.response.send_message(chunk, **kwargs)
            message = await interaction.original_response()
        else:
            # Explicit original edit is handled by loading(); send() means followup.
            message = await interaction.followup.send(chunk, wait=True, **kwargs)
        if result is None:
            result = message
    return result


async def edit(interaction, content):
    if not interaction.response.is_done():
        return await send(interaction, content)
    chunks = split_text(content)
    result = await interaction.edit_original_response(content=chunks[0])
    original_ephemeral = bool(getattr(getattr(result, 'flags', None), 'ephemeral', False))
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=original_ephemeral, wait=True)
    return result


async def loading(interaction, content=None, thinking=True):
    if not interaction.response.is_done():
        return await interaction.response.defer(thinking=thinking)
    if content is not None:
        return await edit(interaction, content)
    # Component updates without text should acknowledge without clearing output.
    return None


async def progress_edit(message, content):
    try:
        if message is not None:
            await message.edit(content=content)
    except Exception as error:
        _progress_failure('edit', error)


async def progress_delete(message):
    try:
        if message is not None:
            await message.delete()
    except Exception as error:
        _progress_failure('delete', error)


async def progress_send(interaction, content):
    try:
        # All current callers defer first. Own the original explicitly rather than
        # relying on Discord's first-followup-after-defer compatibility behavior.
        return await edit(interaction, content)
    except Exception as error:
        _progress_failure('create', error)
        return None


async def channel_send(channel, content, *, valid=lambda: True):
    for chunk in split_text(content):
        if not valid():
            return False
        await channel.send(chunk)
    return valid()
