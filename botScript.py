import logging
import os
import sys
try:
    from dotenv import load_dotenv
except Exception:
    # dotenv not installed; we'll still allow reading os.environ
    load_dotenv = None

from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Updater, CommandHandler, MessageHandler, CallbackContext, CallbackQueryHandler, Filters
from telegram import error as tg_error
from uuid import uuid4
import threading
import re
import time

# In-memory store for pending approvals: approval_id -> {file_ids, caption, poll}
APPROVALS = {}

# Temporary buffer for incoming media groups (albums). Keyed by (chat_id, media_group_id)
# Each value: {'items': [{'file_id': str, 'caption': str or None}], 'timer': threading.Timer}
MEDIA_GROUPS = {}
# How long (seconds) to wait after the last media_group message before processing the group
MEDIA_GROUP_WAIT = 0.8

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load configuration from .env or environment variables
if load_dotenv:
    load_dotenv()

# Define the channel ID where images will be forwarded
CHANNEL_ID = os.environ.get('CHANNEL_ID', 'your_channel_id_here')
OWNER_CHAT_ID = os.environ.get('OWNER_CHAT_ID', 'owner_chat_id')

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')

# Full guide message to show when asking for caption or poll
GUIDE_TEXT = (
    "Send me the caption or poll.\n\n"
    "To add a caption:\n"
    "- Just type the caption text and send it.\n\n"
    "To create a poll:\n"
    "- Start your message with /poll followed by the question and each option separated by a vertical bar |.\n"
    "- Format: /poll Question|Option 1|Option 2|Option 3\n"
    "- Example: /poll Which color do you prefer?|Red|Blue|Green\n\n"
    "Important:\n"
    "- Polls must have at least 2 options (and up to 10).\n"
    "- The first item after /poll is the poll question; the remaining items are the options.\n"
    "- Avoid using | inside option text (use it only as the separator).\n\n"
    "After you send the caption or /poll message the bot will preview the photo with your caption or poll.\n"
    "Tap Confirm to forward it for approval, or New Input to change it."
)

def validate_config():
    missing = []
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == 'YOUR_BOT_TOKEN_HERE':
        missing.append('TELEGRAM_BOT_TOKEN')
    if not CHANNEL_ID or CHANNEL_ID == 'your_channel_id_here':
        missing.append('CHANNEL_ID')
    if not OWNER_CHAT_ID or OWNER_CHAT_ID == 'owner_chat_id':
        missing.append('OWNER_CHAT_ID')
    if missing:
        print('Missing or placeholder configuration for:', ', '.join(missing))
        print('Please update the .env file or set environment variables. See README.md for details.')
        sys.exit(1)


def parse_poll(text: str):
    """Parse a /poll command and return (question, [options]) or None if invalid.

    Accepts formats like:
      /poll Question|Opt1|Opt2
      /poll@botname Question|Opt1|Opt2
      /poll    Question|Opt1|Opt2 (extra spaces)
    """
    if not text:
        return None
    m = re.match(r'^/poll(?:@\S+)?\s*(.*)$', text.strip(), flags=re.I)
    if not m:
        return None
    rest = m.group(1).strip()
    if not rest:
        return None
    parts = [p.strip() for p in rest.split('|')]
    if len(parts) < 2:
        return None
    question = parts[0]
    options = [p for p in parts[1:] if p]
    if len(options) < 2:
        return None
    return question, options


def safe_edit_or_reply(query, text: str):
    """Try to edit the callback message text or caption; fall back to replying if edit fails."""
    try:
        # If message contains photo/media, edit caption instead of text
        if query.message and getattr(query.message, 'photo', None):
            logger.info('safe_edit_or_reply: editing caption for photo message')
            try:
                query.edit_message_caption(caption=text)
                return
            except Exception as e:
                logger.warning('edit_message_caption failed: %s', e)
        # Otherwise, try editing text
        try:
            query.edit_message_text(text=text)
            return
        except Exception as e:
            logger.warning('edit_message_text failed: %s', e)
        # Fallback: send a new message to the user
        try:
            query.message.reply_text(text)
        except Exception as e:
            logger.error('fallback reply_text also failed: %s', e)
    except Exception as e:
        logger.exception('safe_edit_or_reply unexpected error: %s', e)

# Define a command handler. This usually takes the two arguments update and context.
def start(update: Update, context: CallbackContext) -> None:
    logger.info('HANDLER start: user=%s', update.effective_user and update.effective_user.id)
    update.message.reply_text('Hi! Send me images and I will forward it to the channel.')

def handle_image(update: Update, context: CallbackContext) -> None:
    logger.info('HANDLER handle_image: called by user=%s', update.effective_user and update.effective_user.id)
    # Check if the message contains a photo
    if update.message and update.message.photo:
        # If this photo is part of a media group (album), buffer it until the group is complete
        media_group_id = getattr(update.message, 'media_group_id', None)
        if media_group_id:
            logger.info('handle_image: photo belongs to media_group_id=%s', media_group_id)
            _buffer_media_group(update, context)
            return
        # Get the file ID of the photo
        file_id = update.message.photo[-1].file_id
        logger.info('handle_image: got file_id=%s', file_id)
        # Store the file ID in user data
        context.user_data['image_file_id'] = file_id
        logger.info('handle_image: stored image_file_id in user_data')
        # Ask the user if they want to add a caption or poll
        keyboard = [[InlineKeyboardButton("Yes", callback_data='add_caption_poll')],
                    [InlineKeyboardButton("No", callback_data='no_caption_poll')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        update.message.reply_text('Image received. Do you need to add a caption or poll to it?', reply_markup=reply_markup)
        logger.info('handle_image: asked user to add caption/poll')
    else:
        logger.info('handle_image: no photo in message')
        update.message.reply_text('Please send a photo.')


def _buffer_media_group(update: Update, context: CallbackContext) -> None:
    """Buffer an incoming media_group (album) message and schedule processing after a short delay.

    Telegram sends album items as separate messages sharing the same media_group_id. We collect them
    briefly and then process the album as a single approval item.
    """
    msg = update.message
    chat_id = update.effective_chat.id
    mgid = msg.media_group_id
    key = (chat_id, mgid)

    file_id = msg.photo[-1].file_id
    caption = msg.caption if getattr(msg, 'caption', None) else None

    entry = MEDIA_GROUPS.get(key)
    if not entry:
        entry = {'items': [], 'timer': None, 'user_id': update.effective_user and update.effective_user.id}
        MEDIA_GROUPS[key] = entry

    entry['items'].append({'file_id': file_id, 'caption': caption})
    logger.info('Buffered media_group %s: now %d items', mgid, len(entry['items']))

    # Cancel previous timer (if any) and start a new one. When the timer fires we assume the album is complete.
    if entry.get('timer'):
        try:
            entry['timer'].cancel()
        except Exception:
            pass

    timer = threading.Timer(MEDIA_GROUP_WAIT, _process_media_group, args=(chat_id, mgid, context))
    entry['timer'] = timer
    timer.daemon = True
    timer.start()


def _process_media_group(chat_id: int, media_group_id: str, context: CallbackContext) -> None:
    """Called when a buffered media_group should be processed as a single album approval."""
    key = (chat_id, media_group_id)
    entry = MEDIA_GROUPS.pop(key, None)
    if not entry:
        return

    items = entry.get('items', [])
    if not items:
        return

    # Preserve original order
    file_ids = [it['file_id'] for it in items]
    # Try to find a non-empty caption from the album (commonly only one item has caption)
    caption = None
    for it in items:
        if it.get('caption'):
            caption = it['caption']
            break

    logger.info('Processing media_group %s from chat %s with %d items (caption=%s)', media_group_id, chat_id, len(file_ids), bool(caption))

    # Instead of auto-approving, set pending album into the user's context so they can add caption/poll.
    # We try to look up the user's context via the provided CallbackContext. The MEDIA_GROUPS entry saved user_id.
    user_id = entry.get('user_id')
    try:
        # context.user_data is keyed by user id inside the CallbackContext; set album info there
        if user_id:
            udata = context.dispatcher.user_data.get(user_id, {})
            udata['image_file_ids'] = file_ids
            if caption:
                udata['caption'] = caption
            # store back (dispatcher.user_data supports mutation in place)
            context.dispatcher.user_data[user_id] = udata
            logger.info('Stored album in user_data for user=%s items=%d', user_id, len(file_ids))

            # Send preview of the album back to the sender
            media = []
            for i, fid in enumerate(file_ids):
                if i == 0 and caption:
                    media.append(InputMediaPhoto(media=fid, caption=caption))
                else:
                    media.append(InputMediaPhoto(media=fid))
            try:
                context.bot.send_media_group(chat_id=chat_id, media=media)
            except Exception as e:
                logger.exception('Failed to send media_group preview to user: %s', e)

            # Ask the user if they want to add caption/poll (Yes -> add_caption_poll, No -> no_caption_poll)
            keyboard = [[InlineKeyboardButton("Yes", callback_data='add_caption_poll')],
                        [InlineKeyboardButton("No", callback_data='no_caption_poll')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                context.bot.send_message(chat_id=chat_id, text='Album received. Do you need to add a caption or poll to it?', reply_markup=reply_markup)
            except Exception as e:
                logger.exception('Failed to send album action keyboard to user: %s', e)
            return
    except Exception:
        logger.exception('Failed to store album in user_data; falling back to immediate owner send')

    # Fallback: Create approval entry for the album and send to owner immediately
    approval_id = str(uuid4())
    APPROVALS[approval_id] = {'file_ids': file_ids, 'caption': caption, 'poll': None}

    media = []
    for i, fid in enumerate(file_ids):
        if i == 0 and caption:
            media.append(InputMediaPhoto(media=fid, caption=caption))
        else:
            media.append(InputMediaPhoto(media=fid))

    try:
        context.bot.send_media_group(chat_id=OWNER_CHAT_ID, media=media)
    except Exception as e:
        logger.exception('Failed to send media_group to owner: %s', e)

    keyboard = [[InlineKeyboardButton("Approve", callback_data=f'approve:{approval_id}'),
                 InlineKeyboardButton("Disapprove", callback_data=f'disapprove:{approval_id}')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        context.bot.send_message(chat_id=OWNER_CHAT_ID, text='Approval request for album', reply_markup=reply_markup)
    except Exception as e:
        logger.exception('Failed to send approval keyboard for media_group: %s', e)

def button(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()
    data = query.data
    user_id = update.effective_user and update.effective_user.id
    logger.info('HANDLER button: callback from user=%s data=%s', user_id, data)

    if data == 'add_caption_poll':
        logger.info('button: user chose to add caption/poll')
        safe_edit_or_reply(query, GUIDE_TEXT)
        return

    if data == 'no_caption_poll':
        logger.info('button: user chose no caption/poll; creating approval and forwarding to owner')
        # Create approval entry from current user's context
        # Check for album first
        file_ids = context.user_data.get('image_file_ids')
        if file_ids:
            approval_id = str(uuid4())
            APPROVALS[approval_id] = {'file_ids': file_ids, 'caption': None, 'poll': None}
            # send album to owner
            media = [InputMediaPhoto(media=fid) for fid in file_ids]
            try:
                context.bot.send_media_group(chat_id=OWNER_CHAT_ID, media=media)
            except Exception as e:
                logger.exception('Failed to send media_group to owner: %s', e)
            keyboard = [[InlineKeyboardButton("Approve", callback_data=f'approve:{approval_id}'),
                         InlineKeyboardButton("Disapprove", callback_data=f'disapprove:{approval_id}')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            context.bot.send_message(chat_id=OWNER_CHAT_ID, text='Approval request for album', reply_markup=reply_markup)
            # clear user_data for this user
            for k in ('image_file_ids', 'image_file_id', 'caption', 'poll_options', 'poll_question'):
                context.user_data.pop(k, None)
            safe_edit_or_reply(query, "Album forwarded to the channel for approval.")
            return

        file_id = context.user_data.get('image_file_id')
        if not file_id:
            logger.warning('no image_file_id or image_file_ids in user_data when creating approval')
            safe_edit_or_reply(query, 'No image found to forward for approval.')
            return
        approval_id = str(uuid4())
        APPROVALS[approval_id] = {'file_id': file_id, 'caption': None, 'poll': None}
        # send to owner
        keyboard = [[InlineKeyboardButton("Approve", callback_data=f'approve:{approval_id}'),
                     InlineKeyboardButton("Disapprove", callback_data=f'disapprove:{approval_id}')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        context.bot.send_photo(chat_id=OWNER_CHAT_ID, photo=file_id, caption='Approval request', reply_markup=reply_markup)
        for k in ('image_file_ids', 'image_file_id', 'caption', 'poll_options', 'poll_question'):
            context.user_data.pop(k, None)
        safe_edit_or_reply(query, "Image forwarded to the channel for approval.")
        return

    # Confirmation and approval flows
    if data == 'confirm_caption':
        logger.info('button: received confirm_caption')
        # Support album with caption
        if 'image_file_ids' in context.user_data and 'caption' in context.user_data:
            file_ids = context.user_data['image_file_ids']
            caption = context.user_data['caption']
            logger.info('button: confirm_caption creating approval entry for album and forwarding to owner')
            approval_id = str(uuid4())
            APPROVALS[approval_id] = {'file_ids': file_ids, 'caption': caption, 'poll': None}
            media = []
            for i, fid in enumerate(file_ids):
                if i == 0:
                    media.append(InputMediaPhoto(media=fid, caption=caption))
                else:
                    media.append(InputMediaPhoto(media=fid))
            try:
                context.bot.send_media_group(chat_id=OWNER_CHAT_ID, media=media)
            except Exception as e:
                logger.exception('Failed to send media_group to owner: %s', e)
            keyboard = [[InlineKeyboardButton("Approve", callback_data=f'approve:{approval_id}'),
                         InlineKeyboardButton("Disapprove", callback_data=f'disapprove:{approval_id}')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            context.bot.send_message(chat_id=OWNER_CHAT_ID, text='Approval request for album', reply_markup=reply_markup)
            # clear user_data
            for k in ('image_file_ids', 'image_file_id', 'caption', 'poll_options', 'poll_question'):
                context.user_data.pop(k, None)
            safe_edit_or_reply(query, "Album with caption forwarded to the channel for approval.")
            return

        if 'image_file_id' in context.user_data and 'caption' in context.user_data:
            file_id = context.user_data['image_file_id']
            caption = context.user_data['caption']
            logger.info('button: confirm_caption creating approval entry and forwarding to owner')
            approval_id = str(uuid4())
            APPROVALS[approval_id] = {'file_id': file_id, 'caption': caption, 'poll': None}
            keyboard = [[InlineKeyboardButton("Approve", callback_data=f'approve:{approval_id}'),
                         InlineKeyboardButton("Disapprove", callback_data=f'disapprove:{approval_id}')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            context.bot.send_photo(chat_id=OWNER_CHAT_ID, photo=file_id, caption=caption, reply_markup=reply_markup)
            for k in ('image_file_ids', 'image_file_id', 'caption', 'poll_options', 'poll_question'):
                context.user_data.pop(k, None)
            safe_edit_or_reply(query, "Image with caption forwarded to the channel for approval.")
        return

    if data == 'confirm_poll':
        logger.info('button: received confirm_poll')
        # Support poll for albums
        if 'image_file_ids' in context.user_data and 'poll_options' in context.user_data:
            file_ids = context.user_data['image_file_ids']
            poll_question = context.user_data.get('poll_question', 'Poll Question')
            poll_options = context.user_data['poll_options']
            logger.info('button: confirm_poll creating approval entry for album and forwarding to owner')
            approval_id = str(uuid4())
            APPROVALS[approval_id] = {'file_ids': file_ids, 'caption': poll_question, 'poll': poll_options}
            media = []
            for i, fid in enumerate(file_ids):
                if i == 0:
                    media.append(InputMediaPhoto(media=fid, caption=poll_question))
                else:
                    media.append(InputMediaPhoto(media=fid))
            try:
                context.bot.send_media_group(chat_id=OWNER_CHAT_ID, media=media)
            except Exception as e:
                logger.exception('Failed to send media_group to owner: %s', e)
            keyboard = [[InlineKeyboardButton("Approve", callback_data=f'approve:{approval_id}'),
                         InlineKeyboardButton("Disapprove", callback_data=f'disapprove:{approval_id}')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            context.bot.send_message(chat_id=OWNER_CHAT_ID, text='Approval request for album (poll)', reply_markup=reply_markup)
            for k in ('image_file_ids', 'image_file_id', 'caption', 'poll_options', 'poll_question'):
                context.user_data.pop(k, None)
            safe_edit_or_reply(query, "Album with poll forwarded to the channel for approval.")
            return

        if 'image_file_id' in context.user_data and 'poll_options' in context.user_data:
            file_id = context.user_data['image_file_id']
            poll_question = context.user_data.get('poll_question', 'Poll Question')
            poll_options = context.user_data['poll_options']
            logger.info('button: confirm_poll creating approval entry and forwarding to owner')
            approval_id = str(uuid4())
            APPROVALS[approval_id] = {'file_id': file_id, 'caption': poll_question, 'poll': poll_options}
            keyboard = [[InlineKeyboardButton("Approve", callback_data=f'approve:{approval_id}'),
                         InlineKeyboardButton("Disapprove", callback_data=f'disapprove:{approval_id}')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            context.bot.send_photo(chat_id=OWNER_CHAT_ID, photo=file_id, caption=poll_question, reply_markup=reply_markup)
            for k in ('image_file_ids', 'image_file_id', 'caption', 'poll_options', 'poll_question'):
                context.user_data.pop(k, None)
            safe_edit_or_reply(query, "Image with poll forwarded to the channel for approval.")
        return

    if data == 'new_input':
        logger.info('button: received new_input')
        safe_edit_or_reply(query, GUIDE_TEXT)
        return

    if data == 'approve':
        logger.info('button: received approve without id — ignoring')
        return

    if data == 'disapprove':
        logger.info('button: received disapprove without id — ignoring')
        return

    # Handle owner approve/disapprove with approval_id encoded
    if data and data.startswith('approve:'):
        approval_id = data.split(':', 1)[1]
        logger.info('button: owner approve for id=%s', approval_id)
        approval = APPROVALS.pop(approval_id, None)
        if not approval:
            safe_edit_or_reply(query, 'Approval item not found or already processed.')
            return
        # If it's an album approval
        file_ids = approval.get('file_ids')
        if file_ids:
            caption = approval.get('caption')
            poll = approval.get('poll')
            forward_to_channel(context, file_ids, caption, caption if poll else None, poll)
            safe_edit_or_reply(query, 'Album approved and forwarded to the channel.')
            return

        file_id = approval.get('file_id')
        caption = approval.get('caption')
        poll = approval.get('poll')
        forward_to_channel(context, file_id, caption, caption if poll else None, poll)
        safe_edit_or_reply(query, 'Message approved and forwarded to the channel.')
        return

    if data and data.startswith('disapprove:'):
        approval_id = data.split(':', 1)[1]
        logger.info('button: owner disapprove for id=%s', approval_id)
        approval = APPROVALS.pop(approval_id, None)
        safe_edit_or_reply(query, 'Message disapproved.')
        return
        return

def handle_caption_poll(update: Update, context: CallbackContext) -> None:
    logger.info('HANDLER handle_caption_poll: called by user=%s', update.effective_user and update.effective_user.id)
    # Some updates may be commands delivered without a message body; guard against None
    if not update or not getattr(update, 'message', None) or not getattr(update.message, 'text', None):
        logger.warning('handle_caption_poll: no message text found in update; ignoring')
        return
    user_input = update.message.text
    logger.info('handle_caption_poll: user_input=%s', user_input)
    # First check for album in user_data
    if 'image_file_ids' in context.user_data:
        file_ids = context.user_data['image_file_ids']
        # Use parse_poll which handles variations of /poll command more robustly
        parsed = parse_poll(user_input)
        if parsed:
            poll_question, poll_options = parsed
            if len(poll_options) < 2:
                update.message.reply_text('Please provide at least two options for the poll, separated by |.')
                return
            logger.info('handle_caption_poll: sending album preview with parsed poll question')
            media = []
            for i, fid in enumerate(file_ids):
                if i == 0:
                    media.append(InputMediaPhoto(media=fid, caption=poll_question))
                else:
                    media.append(InputMediaPhoto(media=fid))
            try:
                context.bot.send_media_group(chat_id=update.effective_chat.id, media=media)
            except Exception as e:
                logger.exception('handle_caption_poll: failed to send album preview: %s', e)
            # store parsed poll into user_data and send confirmation keyboard
            context.user_data['poll_options'] = poll_options
            context.user_data['poll_question'] = poll_question
            keyboard = [[InlineKeyboardButton("Confirm", callback_data='confirm_poll'),
                         InlineKeyboardButton("New Input", callback_data='new_input')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                context.bot.send_message(chat_id=update.effective_chat.id, text='Preview: album with poll. Confirm or provide new input.', reply_markup=reply_markup)
            except Exception:
                # fallback to safe edit style reply
                update.message.reply_text('Preview: album with poll. Confirm or provide new input.')
            return

        # Treat as caption
        logger.info('handle_caption_poll: sending album preview with caption')
        media = []
        for i, fid in enumerate(file_ids):
            if i == 0:
                media.append(InputMediaPhoto(media=fid, caption=user_input))
            else:
                media.append(InputMediaPhoto(media=fid))
        try:
            context.bot.send_media_group(chat_id=update.effective_chat.id, media=media)
        except Exception as e:
            logger.exception('handle_caption_poll: failed to send album preview: %s', e)
        context.user_data['caption'] = user_input
        keyboard = [[InlineKeyboardButton("Confirm", callback_data='confirm_caption'),
                     InlineKeyboardButton("New Input", callback_data='new_input')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            context.bot.send_message(chat_id=update.effective_chat.id, text='Preview: album caption. Confirm or provide new input.', reply_markup=reply_markup)
        except Exception:
            update.message.reply_text('Preview: album caption. Confirm or provide new input.')
        return

    # Fallback: single-image flow
    if 'image_file_id' in context.user_data:
        file_id = context.user_data['image_file_id']
        # Use parse_poll for single-image poll parsing as well
        parsed = parse_poll(user_input)
        if parsed:
            poll_question, poll_options = parsed
            if len(poll_options) < 2:
                update.message.reply_text('Please provide at least two options for the poll, separated by |.')
                return
            keyboard = [[InlineKeyboardButton("Confirm", callback_data='confirm_poll'),
                         InlineKeyboardButton("New Input", callback_data='new_input')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            logger.info('handle_caption_poll: sending photo preview with parsed poll question')
            try:
                context.bot.send_photo(chat_id=update.effective_chat.id, photo=file_id, caption=poll_question)
            except Exception as e:
                logger.exception('handle_caption_poll: failed to send photo preview with poll question: %s', e)
            # store poll details and send explicit confirm/new input keyboard
            context.user_data['poll_options'] = poll_options
            context.user_data['poll_question'] = poll_question
            try:
                context.bot.send_message(chat_id=update.effective_chat.id, text='Preview: photo with poll. Confirm or provide new input.', reply_markup=reply_markup)
            except Exception:
                update.message.reply_text('Preview: photo with poll. Confirm or provide new input.')
            return

        # Otherwise treat as caption
        keyboard = [[InlineKeyboardButton("Confirm", callback_data='confirm_caption'),
                     InlineKeyboardButton("New Input", callback_data='new_input')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        logger.info('handle_caption_poll: sending photo preview with caption')
        try:
            context.bot.send_photo(chat_id=update.effective_chat.id, photo=file_id, caption=user_input)
        except Exception as e:
            logger.exception('handle_caption_poll: failed to send photo preview with caption: %s', e)
        context.user_data['caption'] = user_input
        try:
            context.bot.send_message(chat_id=update.effective_chat.id, text='Preview: photo caption. Confirm or provide new input.', reply_markup=reply_markup)
        except Exception:
            update.message.reply_text('Preview: photo caption. Confirm or provide new input.')
    else:
        update.message.reply_text('No image found. Please send an image first.')


def forward_to_owner(update: Update, context: CallbackContext, caption: str = None, poll: list = None) -> None:
    # Support forwarding single photo or album to owner for quick usage (not used in album flow primarily)
    # Try album first
    if 'image_file_ids' in context.user_data:
        file_ids = context.user_data['image_file_ids']
        logger.info('forward_to_owner: sending album to owner=%s file_count=%d caption=%s poll=%s', OWNER_CHAT_ID, len(file_ids), bool(caption), bool(poll))
        media = []
        for i, fid in enumerate(file_ids):
            if i == 0 and caption:
                media.append(InputMediaPhoto(media=fid, caption=caption))
            else:
                media.append(InputMediaPhoto(media=fid))
        try:
            context.bot.send_media_group(chat_id=OWNER_CHAT_ID, media=media)
        except Exception as e:
            logger.exception('forward_to_owner: failed to send media_group: %s', e)
        keyboard = [[InlineKeyboardButton("Approve", callback_data='approve'),
                     InlineKeyboardButton("Disapprove", callback_data='disapprove')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        context.bot.send_message(chat_id=OWNER_CHAT_ID, text='Approval request for album', reply_markup=reply_markup)
        return

    # Fallback to single photo
    file_id = context.user_data.get('image_file_id')
    logger.info('forward_to_owner: sending to owner=%s file_id=%s caption=%s poll=%s', OWNER_CHAT_ID, file_id, bool(caption), bool(poll))
    if caption:
        keyboard = [[InlineKeyboardButton("Approve", callback_data='approve'),
                     InlineKeyboardButton("Disapprove", callback_data='disapprove')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        context.bot.send_photo(chat_id=OWNER_CHAT_ID, photo=file_id, caption=caption, reply_markup=reply_markup)
    elif poll:
        poll_question = poll[0]
        poll_options = poll[1:]
        keyboard = [[InlineKeyboardButton("Approve", callback_data='approve'),
                     InlineKeyboardButton("Disapprove", callback_data='disapprove')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        context.bot.send_poll(chat_id=OWNER_CHAT_ID, question=poll_question, options=poll_options, photo=file_id, reply_markup=reply_markup)

def forward_to_channel(context: CallbackContext, file_id_or_list, caption: str = None, poll_question: str = None, poll_options: list = None) -> None:
    """Forward single photo or album to the channel.

    file_id_or_list may be a single file_id (str) or a list of file_ids for albums.
    If poll_question and poll_options are provided, a poll will be sent after the media.
    """
    is_album = isinstance(file_id_or_list, (list, tuple))
    logger.info('forward_to_channel: sending to channel=%s is_album=%s caption=%s poll_question=%s', CHANNEL_ID, is_album, bool(caption), poll_question)
    if is_album:
        file_ids = list(file_id_or_list)
        media = []
        for i, fid in enumerate(file_ids):
            if i == 0 and caption:
                media.append(InputMediaPhoto(media=fid, caption=caption))
            else:
                media.append(InputMediaPhoto(media=fid))
        try:
            context.bot.send_media_group(chat_id=CHANNEL_ID, media=media)
        except Exception as e:
            logger.exception('forward_to_channel: failed to send media_group: %s', e)
        # If there's a poll, send it after the album
        if poll_question and poll_options:
            # Retry sending poll a few times in case of transient network/read timeouts
            for attempt in range(3):
                try:
                    context.bot.send_poll(chat_id=CHANNEL_ID, question=poll_question, options=poll_options)
                    break
                except tg_error.TimedOut:
                    logger.warning('forward_to_channel: send_poll timed out (attempt %d), retrying...', attempt + 1)
                    time.sleep(1)
                    continue
                except Exception as e:
                    logger.exception('forward_to_channel: failed to send poll for album: %s', e)
                    break
        return

    # Single photo path
    file_id = file_id_or_list
    if poll_question and poll_options:
        try:
            context.bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=caption)
        except Exception as e:
            logger.exception('forward_to_channel: failed to send photo before poll: %s', e)
        # Retry sending poll a few times for transient errors
        for attempt in range(3):
            try:
                context.bot.send_poll(chat_id=CHANNEL_ID, question=poll_question, options=poll_options)
                break
            except tg_error.TimedOut:
                logger.warning('forward_to_channel: send_poll timed out (attempt %d), retrying...', attempt + 1)
                time.sleep(1)
                continue
            except Exception as e:
                logger.exception('forward_to_channel: failed to send poll: %s', e)
                break
        return

    # Simple single photo with optional caption
    try:
        context.bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=caption)
    except Exception as e:
        logger.exception('forward_to_channel: failed to send photo: %s', e)

def main() -> None:
    # Validate config and create the Updater using the token from the environment
    validate_config()
    # Create the Updater and pass it your bot's token.
    # Increase read_timeout to reduce transient ReadTimeout errors when sending polls/media
    updater = Updater(TELEGRAM_BOT_TOKEN, request_kwargs={'read_timeout': 15, 'connect_timeout': 10})

    # Get the dispatcher to register handlers
    dispatcher = updater.dispatcher

    # on different commands - answer in Telegram
    dispatcher.add_handler(CommandHandler("start", start))

    # on non-command i.e message - echo the message on Telegram
    dispatcher.add_handler(MessageHandler(Filters.photo, handle_image))
    # Text messages that are not commands still go to handle_caption_poll
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_caption_poll))
    # Also explicitly handle /poll commands (commands are filtered out by the above handler)
    dispatcher.add_handler(CommandHandler('poll', handle_caption_poll))
    dispatcher.add_handler(CallbackQueryHandler(button))

    # Start the Bot
    updater.start_polling()

    # Run the bot until you press Ctrl-C or the process receives SIGINT, SIGTERM or SIGABRT
    updater.idle()

if __name__ == '__main__':
    main()