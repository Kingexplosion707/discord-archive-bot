const { SlashCommandBuilder, ChannelType, PermissionFlagsBits, InteractionContextType, ApplicationIntegrationType } = require('discord.js');

// Single source of truth for the slash commands. The bot registers these
// itself on startup (index.js), so a deploy is all it takes — running
// `npm run deploy-commands` by hand is only needed for a GUILD_ID test server.
// Every command is server-only (no DMs) and installed with the bot to a server.
const base = (name, description) =>
  new SlashCommandBuilder()
    .setName(name)
    .setDescription(description)
    .setContexts(InteractionContextType.Guild)
    .setIntegrationTypes(ApplicationIntegrationType.GuildInstall);

// view / add / remove for a personal list
const listCommand = (name, description, emoji) =>
  base(name, description)
    .addSubcommand((sc) => sc.setName('view').setDescription(`${emoji} Open it — reorder, notes, remove`))
    .addSubcommand((sc) => {
      sc.setName('add')
        .setDescription(`${emoji} Add a movie or show`)
        .addStringOption((o) => o.setName('title').setDescription('Start typing a title…').setRequired(true).setAutocomplete(true));
      if (name !== 'rotation') sc.addStringOption((o) => o.setName('note').setDescription('Optional note').setRequired(false).setMaxLength(140));
      return sc;
    })
    .addSubcommand((sc) =>
      sc
        .setName('remove')
        .setDescription(`${emoji} Remove a movie or show`)
        .addStringOption((o) => o.setName('title').setDescription('Pick from your list').setRequired(true).setAutocomplete(true))
    );

const { CATEGORIES } = require('./features/categories');
const MUTE_SCOPES = [
  { name: 'My posts — don’t post it (still counted in my stats)', value: 'posts' },
  { name: 'My tracking — don’t post or count it at all', value: 'track' },
  { name: 'This server — never post it here (Manage Server)', value: 'server' },
];
const muteOptions = (sc) =>
  sc
    .addStringOption((o) =>
      o.setName('category').setDescription('What to mute').setRequired(true).addChoices(...CATEGORIES.map((c) => ({ name: `${c.emoji} ${c.label}`, value: c.key })))
    )
    .addStringOption((o) => o.setName('where').setDescription('Your posts, your tracking, or this whole server').setRequired(true).addChoices(...MUTE_SCOPES));

const commands = [
  base('track', 'Post a signed-in person’s movie and TV activity in this server').addUserOption((opt) =>
    opt.setName('user').setDescription('Someone who has used /signin').setRequired(true)
  ),
  base('untrack', 'Stop posting someone’s activity in this server').addStringOption((opt) =>
    opt.setName('username').setDescription('Their Simkl name, as shown in /list').setRequired(true).setMaxLength(50)
  ),
  base('list', 'List the people tracked in this server'),
  base('whostracking', 'Who is being tracked and by whom (never pings anyone)'),
  base('setchannel', 'Choose the channel where watch posts go')
    .setDefaultMemberPermissions(PermissionFlagsBits.ManageGuild)
    .addChannelOption((opt) =>
      opt
        .setName('channel')
        .setDescription('Text channel for activity posts')
        .setRequired(true)
        .addChannelTypes(ChannelType.GuildText, ChannelType.GuildAnnouncement)
    ),
  listCommand('watchlater', 'Your Watch Later backlog', '📌'),
  listCommand('myfavorites', 'Your all-time favorites', '⭐'),
  listCommand('rotation', 'What you’re watching right now (In Rotation)', '🔄'),
  base('find', 'Look up any movie or show')
    .addSubcommand((sc) =>
      sc
        .setName('info')
        .setDescription('Details, rating, trailer — plus quick actions')
        .addStringOption((o) => o.setName('title').setDescription('Start typing a title…').setRequired(true).setAutocomplete(true))
        .addBooleanOption((o) => o.setName('share').setDescription('Post it in the channel instead of just for you').setRequired(false))
    )
    .addSubcommand((sc) =>
      sc
        .setName('search')
        .setDescription('Search Simkl by name')
        .addStringOption((o) => o.setName('query').setDescription('What to search for').setRequired(true).setMaxLength(80))
        .addBooleanOption((o) => o.setName('share').setDescription('Post it in the channel').setRequired(false))
    )
    .addSubcommand((sc) =>
      sc
        .setName('trending')
        .setDescription('What’s trending, most popular or highest rated')
        .addStringOption((o) =>
          o
            .setName('sort')
            .setDescription('Which list')
            .setRequired(false)
            .addChoices({ name: '🔥 Trending now', value: 'TRENDING_DESC' }, { name: '❤️ Most popular', value: 'POPULARITY_DESC' }, { name: '🏆 Highest rated', value: 'SCORE_DESC' })
        )
        .addBooleanOption((o) => o.setName('share').setDescription('Post it in the channel').setRequired(false))
    )
    .addSubcommand((sc) =>
      sc
        .setName('random')
        .setDescription('Can’t decide? Get a random popular movie or show')
        .addStringOption((o) =>
          o
            .setName('genre')
            .setDescription('Only this genre')
            .setRequired(false)
            .addChoices(
              ...['Action', 'Adventure', 'Animation', 'Comedy', 'Crime', 'Documentary', 'Drama', 'Family', 'Fantasy', 'History', 'Horror', 'Mystery', 'Romance', 'Science Fiction', 'Thriller', 'War'].map((g) => ({ name: g, value: g }))
            )
        )
        .addBooleanOption((o) => o.setName('share').setDescription('Post it in the channel').setRequired(false))
    ),
  base('mostrecommended', 'The most-recommended movies and shows — this server or global'),
  base('mute', 'Stop posts or tracking for a category, like anything anime')
    .addSubcommand((sc) => muteOptions(sc.setName('add').setDescription('Mute a category')))
    .addSubcommand((sc) => muteOptions(sc.setName('remove').setDescription('Unmute a category')))
    .addSubcommand((sc) => sc.setName('list').setDescription('What you and this server have muted')),
  base('signin', 'Link your Simkl account so only you control your posts'),
  base('signout', 'Unlink your Simkl account from TV Tracker'),
  base('tracking', 'Turn your live watch posts on or off (everywhere)').addStringOption((opt) =>
    opt
      .setName('status')
      .setDescription('Leave empty to see your current setting')
      .setRequired(false)
      .addChoices({ name: 'On — post my activity', value: 'on' }, { name: 'Off — stop posting my activity', value: 'off' })
  ),
  base('update', 'Post an update notice in this server silently (no notifications)')
    .setDefaultMemberPermissions(PermissionFlagsBits.ManageGuild)
    .addStringOption((opt) =>
      opt.setName('message').setDescription('Custom message — leave empty to post the latest TV Tracker changelog').setRequired(false).setMaxLength(1800)
    )
    .addChannelOption((opt) =>
      opt
        .setName('channel')
        .setDescription('Where to post (defaults to the watch post channel)')
        .setRequired(false)
        .addChannelTypes(ChannelType.GuildText, ChannelType.GuildAnnouncement)
    ),
  base('profile', 'Open a movie & TV profile — favorites, reviews, stats and more').addUserOption((o) =>
    o.setName('user').setDescription('Whose profile (leave empty for yours)').setRequired(false)
  ),
  base('settings', 'Customize your profile, theme, activity posts and privacy').addStringOption((o) =>
    o
      .setName('section')
      .setDescription('Jump straight to a section')
      .setRequired(false)
      .addChoices(
        { name: '🎨 Appearance', value: 'look' },
        { name: '👤 Profile', value: 'profile' },
        { name: '🧩 Showcase', value: 'showcase' },
        { name: '📡 Activity posts', value: 'activity' },
        { name: '🔒 Privacy', value: 'privacy' },
        { name: '📊 Stats & reviews', value: 'more' },
        { name: '🛡️ Server', value: 'server' }
      )
  ),
  base('review', 'Write and read movie and TV reviews')
    .addSubcommand((sc) =>
      sc
        .setName('write')
        .setDescription('Write (or edit) your review of a movie or show')
        .addStringOption((o) => o.setName('title').setDescription('Start typing a title…').setRequired(true).setAutocomplete(true))
    )
    .addSubcommand((sc) =>
      sc
        .setName('read')
        .setDescription('Read reviews of a movie or show')
        .addStringOption((o) => o.setName('title').setDescription('Start typing a title…').setRequired(true).setAutocomplete(true))
    )
    .addSubcommand((sc) =>
      sc
        .setName('browse')
        .setDescription('Discover reviews — newest, most helpful, highest rated, this server')
        .addStringOption((o) =>
          o
            .setName('sort')
            .setDescription('How to sort')
            .setRequired(false)
            .addChoices(
              { name: '🆕 Newest', value: 'recent' },
              { name: '❤️ Most helpful', value: 'liked' },
              { name: '🏆 Highest rated', value: 'rating' },
              { name: '🏠 This server', value: 'server' },
              { name: '✍️ My reviews', value: 'mine' }
            )
        )
    ),
  base('collection', 'Your lists — favorites, watch later, custom collections')
    .addSubcommand((sc) =>
      sc
        .setName('view')
        .setDescription('Open a collection')
        .addUserOption((o) => o.setName('user').setDescription('Whose (leave empty for yours)').setRequired(false))
        .addStringOption((o) => o.setName('collection').setDescription('Which list').setRequired(false).setAutocomplete(true))
    )
    .addSubcommand((sc) =>
      sc
        .setName('add')
        .setDescription('Add a movie or show to one of your lists')
        .addStringOption((o) => o.setName('title').setDescription('Start typing a title…').setRequired(true).setAutocomplete(true))
        .addStringOption((o) => o.setName('collection').setDescription('Which list (leave empty to pick several)').setRequired(false).setAutocomplete(true))
    )
    .addSubcommand((sc) => sc.setName('create').setDescription('Create a custom collection, like “Comfort Movies”'))
    .addSubcommand((sc) => sc.setName('manage').setDescription('Reorder, add notes, rename or delete your lists')),
  base('recommend', 'Recommend a movie or show to everyone, with an optional message')
    .addStringOption((o) => o.setName('title').setDescription('Start typing a title…').setRequired(true).setAutocomplete(true))
    .addStringOption((o) => o.setName('message').setDescription('Why people should watch it (no spoilers!)').setRequired(false).setMaxLength(140)),
  base('stats', 'Watching stats — this month, lifetime and streaks').addUserOption((o) =>
    o.setName('user').setDescription('Whose stats (leave empty for yours)').setRequired(false)
  ),
  base('recap', 'Your monthly movie & TV recap — shareable')
    .addStringOption((o) => o.setName('month').setDescription('Which month (default: this month)').setRequired(false).setAutocomplete(true))
    .addUserOption((o) => o.setName('user').setDescription('Whose recap').setRequired(false))
    .addBooleanOption((o) => o.setName('private').setDescription('Only show it to me').setRequired(false)),
  base('leaderboard', 'Server leaderboards — episodes, streaks, reviews and more').addStringOption((o) =>
    o
      .setName('category')
      .setDescription('Which leaderboard')
      .setRequired(false)
      .addChoices(
        { name: '📺 Episodes this month', value: 'episodes' },
        { name: '🔥 Current streak', value: 'streak' },
        { name: '✅ Completed this month', value: 'completed' },
        { name: '💜 Recommendations', value: 'recommendations' },
        { name: '✍️ Reviews written', value: 'reviews' },
        { name: '❤️ Most helpful reviewers', value: 'helpful' },
        { name: '⭐ Most titles rated', value: 'ratings' },
        { name: '😳 Freakiest in the server', value: 'freaky' }
      )
  ),
  base('rate', 'Give a movie or show a score from 1 to 10 (no review needed)')
    .addStringOption((o) => o.setName('title').setDescription('Start typing a title…').setRequired(true).setAutocomplete(true))
    .addIntegerOption((o) => o.setName('score').setDescription('Your score').setRequired(true).setMinValue(1).setMaxValue(10))
    .addStringOption((o) => o.setName('note').setDescription('One-line take (no spoilers!)').setRequired(false).setMaxLength(120)),
  base('ratings', 'Ratings — the server’s top rated, one person’s page, or one title')
    .addUserOption((o) => o.setName('user').setDescription('See this person’s ratings page').setRequired(false))
    .addStringOption((o) => o.setName('title').setDescription('See everyone’s rating of this title').setRequired(false).setAutocomplete(true)),
  base('watched', 'Find what people in this server watched')
    .addStringOption((o) => o.setName('title').setDescription('Who here watched this title').setRequired(false).setAutocomplete(true))
    .addUserOption((o) => o.setName('user').setDescription('What this person watched').setRequired(false))
    .addStringOption((o) => o.setName('search').setDescription('Search titles (or tags) the server watched').setRequired(false).setMaxLength(60)),
  base('serversettings', 'Admin panel: channel, recaps, leaderboards, auto-tracking and more').setDefaultMemberPermissions(PermissionFlagsBits.ManageGuild),
  base('feedback', 'Rate TV Tracker and send feedback to the developer'),
  base('help', 'All TV Tracker commands and buttons'),
].map((c) => c.toJSON());

module.exports = { commands };
