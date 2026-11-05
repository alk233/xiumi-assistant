/**
 * 秀米助手设置页（DSH Web UI client 半）。
 *
 * - 不写 JSX：DSH 的 client bundle 不经过 JSX 转换，全部用 React.createElement。
 * - 只通过同源 HTTP 与 host 通信：/__xiumi/config、/__xiumi/theme、/__xiumi/login 等。
 * - 主题表不在客户端复制一份，而是从 host 的 /__xiumi/theme 拉取，保证预览与
 *   xiumi_write 使用同一套风格/配色定义。
 */
window.__ModuleLoader__.load({
  id: '@local/xiumi-assistant',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports
    var React = require('react')
    var h = React.createElement
    var useState = React.useState
    var useEffect = React.useEffect
    var useMemo = React.useMemo

    var inject = ['slots']
    var API = '/__xiumi'
    var PREVIEW_TITLE = '山野来信'
    var PREVIEW_PARAGRAPHS = [
      '清晨的风掠过窗棂，把一夜的安静轻轻叠好。日子并不总是盛大，更多时候，它藏在一杯温茶和一段慢走里。',
      '把脚步放慢一点，才能听见生活细微的回声。愿你在忙碌之间，仍有一块属于自己的留白。',
    ]

    async function request(path, options) {
      var opts = options || {}
      var response = await fetch(API + path, {
        method: opts.method || 'GET',
        headers: opts.body
          ? { 'Content-Type': 'application/json' }
          : undefined,
        body: opts.body,
        cache: 'no-store',
        credentials: 'same-origin',
      })
      var data = null
      try {
        data = await response.json()
      } catch {
        data = null
      }
      if (!response.ok || (data && data.ok === false)) {
        throw new Error(
          (data && data.error) || '请求失败（HTTP ' + response.status + '）',
        )
      }
      return data || {}
    }

    function errorText(error) {
      return error instanceof Error ? error.message : '未知错误'
    }

    function cn() {
      var parts = []
      for (var i = 0; i < arguments.length; i += 1) {
        if (arguments[i]) parts.push(arguments[i])
      }
      return parts.join(' ')
    }

    function findStyle(theme, id) {
      if (!theme) return null
      for (var i = 0; i < theme.styles.length; i += 1) {
        if (theme.styles[i].id === id) return theme.styles[i]
      }
      return null
    }

    function findColor(theme, id) {
      if (!theme) return null
      for (var i = 0; i < theme.colors.length; i += 1) {
        if (theme.colors[i].id === id) return theme.colors[i]
      }
      return null
    }

    function buildLocalTheme(theme, styleId, colorId) {
      var style = findStyle(theme, styleId) || findStyle(theme, 'minimal')
      var color = findColor(theme, colorId) || findColor(theme, 'vermilion')
      if (!style || !color) return null
      return {
        style: style,
        color: color,
        dark: Boolean(style.dark),
        background: style.dark ? '#141414' : color.background,
        text: style.dark ? '#F4F4F6' : color.text,
      }
    }

    function cardBorder(style, dark) {
      if (!style || !style.card || style.card.border === 'none' || !style.card.width) {
        return 'none'
      }
      return (
        String(style.card.width) +
        'px ' +
        style.card.border +
        ' ' +
        (dark ? 'rgba(255,255,255,.28)' : 'rgba(0,0,0,.18)')
      )
    }

    function Divider(props) {
      var style = props.style
      var color = props.color
      if (!style || style.divider === 'none') return null
      var base = { margin: '14px 0 18px', height: '1px', background: color + '59' }
      if (style.divider === 'double') {
        return h('div', {
          style: {
            margin: '14px 0 18px',
            height: 0,
            borderTop: '3px double ' + color,
            opacity: 0.8,
          },
        })
      }
      if (style.divider === 'dashed') {
        return h('div', {
          style: {
            margin: '14px 0 18px',
            height: 0,
            borderTop: '2px dashed ' + color,
            opacity: 0.65,
          },
        })
      }
      if (style.divider === 'grid') {
        return h('div', {
          style: {
            margin: '14px 0 18px',
            height: '7px',
            background:
              'repeating-linear-gradient(90deg,' +
              color +
              ' 0 1px,transparent 1px 12px)',
            opacity: 0.55,
          },
        })
      }
      if (style.divider === 'pattern') {
        return h('div', {
          style: {
            margin: '14px 0 18px',
            height: '9px',
            background:
              'repeating-linear-gradient(45deg,' +
              color +
              ' 0 2px,transparent 2px 9px)',
            opacity: 0.5,
          },
        })
      }
      return h('div', { style: base })
    }

    function Accent(props) {
      var style = props.style
      var color = props.color
      if (style.accent === 'bar') {
        return h('div', {
          style: {
            width: 58,
            height: 5,
            borderRadius: 99,
            background: color,
            margin: '12px 0 14px',
          },
        })
      }
      if (style.accent === 'gradient') {
        return h('div', {
          style: {
            width: '100%',
            height: 5,
            borderRadius: 99,
            background: 'linear-gradient(90deg,' + color + ',transparent)',
            margin: '12px 0 14px',
          },
        })
      }
      if (style.accent === 'block') {
        return h(
          'div',
          {
            style: {
              display: 'inline-block',
              background: color,
              color: '#fff',
              fontSize: 11,
              letterSpacing: 1,
              padding: '4px 9px',
              borderRadius: 4,
              margin: '10px 0 12px',
            },
          },
          '主题强调 · 重点',
        )
      }
      if (style.accent === 'stamp') {
        return h(
          'div',
          {
            style: {
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              width: 42,
              height: 42,
              border: '2px solid ' + color,
              color: color,
              fontSize: 15,
              fontWeight: 700,
              letterSpacing: 2,
              transform: 'rotate(-4deg)',
              margin: '10px 0 12px',
              fontFamily: '"Songti SC","STSong",serif',
            },
          },
          '印记',
        )
      }
      return null
    }

    function Preview(props) {
      var theme = props.theme
      var title = props.title
      if (!theme) {
        return h(
          'div',
          { style: { fontSize: 12, opacity: 0.6, padding: 16 } },
          '正在加载预览…',
        )
      }
      var style = theme.style
      var color = theme.color
      var primary = color.primary
      var textColor = theme.text
      var dark = theme.dark
      var titleColor =
        style.title.color === 'primary' ? primary : textColor
      var bodyStyle = {
        fontFamily: style.body.family,
        fontSize: style.body.size,
        lineHeight: style.body.lineHeight,
        color: textColor,
        margin: '0 0 ' + style.body.paragraphSpacing + 'px',
        textAlign: 'justify',
      }
      var titleStyle = {
        fontFamily: style.title.family,
        fontSize: style.title.size,
        fontWeight: style.title.weight,
        lineHeight: style.title.lineHeight,
        letterSpacing: style.title.letterSpacing,
        color: titleColor,
        textAlign: style.title.align,
        margin: '0 0 8px',
        wordBreak: 'break-word',
      }

      var content = []
      content.push(
        h(
          'div',
          { key: 'title', style: titleStyle },
          title || PREVIEW_TITLE,
        ),
      )
      content.push(
        h(Divider, { key: 'divider', style: style, color: primary }),
      )
      content.push(h(Accent, { key: 'accent', style: style, color: primary }))

      var paragraphs = []
      if (style.numbering) {
        paragraphs.push(
          h(
            'div',
            {
              key: 'num',
              style: {
                display: 'flex',
                gap: 8,
                alignItems: 'baseline',
                margin: '2px 0 10px',
                color: primary,
                fontFamily: style.title.family,
                fontSize: Math.max(11, style.body.size - 2),
                fontWeight: 700,
                letterSpacing: 1,
              },
            },
            [
              h('span', { key: 'n' }, '01'),
              h('span', { key: 't' }, '观察 · 记录'),
            ],
          ),
        )
      }

      PREVIEW_PARAGRAPHS.forEach(function (paragraph, index) {
        var pStyle = Object.assign({}, bodyStyle)
        if (style.quote === 'left' && index === 0) {
          pStyle.borderLeft = '3px solid ' + primary
          pStyle.paddingLeft = 12
          pStyle.color = dark ? '#E7E7EA' : color.text
          pStyle.fontStyle = 'italic'
        }
        if (style.quote === 'indent') {
          pStyle.textIndent = '2em'
        }
        if (style.quote === 'box') {
          pStyle.background = dark ? 'rgba(255,255,255,.06)' : primary + '0D'
          pStyle.borderLeft = '3px solid ' + primary
          pStyle.padding = '10px 12px'
          pStyle.borderRadius = style.card.radius ? 6 : 0
        }
        paragraphs.push(
          h('p', { key: 'p' + index, style: pStyle }, paragraph),
        )
      })

      if (style.columns) {
        content.push(
          h(
            'div',
            {
              key: 'columns',
              style: { columnCount: 2, columnGap: 22 },
            },
            paragraphs,
          ),
        )
      } else {
        content.push(
          h('div', { key: 'body' }, paragraphs),
        )
      }

      if (style.sticker) {
        content.push(
          h(
            'div',
            {
              key: 'sticker',
              style: {
                position: 'absolute',
                right: 18,
                bottom: 18,
                width: 42,
                height: 42,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                background: primary,
                color: '#fff',
                fontSize: 13,
                borderRadius: '50% 50% 50% 8px',
                transform: 'rotate(8deg)',
                boxShadow: '0 4px 12px rgba(0,0,0,.15)',
              },
            },
            '贴',
          ),
        )
      }

      var paperOverlay = style.paper
        ? 'linear-gradient(rgba(255,246,226,.55),rgba(255,246,226,.55))'
        : undefined

      return h(
        'div',
        {
          style: {
            position: 'relative',
            background: theme.background,
            backgroundImage: paperOverlay,
            color: textColor,
            border: cardBorder(style, dark),
            borderRadius: style.card.radius,
            padding: style.card.padding,
            boxShadow: style.card.shadow
              ? '0 8px 24px rgba(0,0,0,.10)'
              : 'none',
            overflow: 'hidden',
          },
        },
        content,
      )
    }

    function styleThumb(style, color) {
      var dark = Boolean(style.dark)
      var fg = dark ? '#F4F4F6' : color.text
      var bg = dark ? '#141414' : color.background
      var border =
        style.card.border === 'none'
          ? '1px solid rgba(127,127,127,.18)'
          : String(style.card.width) +
            'px ' +
            style.card.border +
            ' ' +
            color.primary
      return h(
        'div',
        {
          style: {
            background: bg,
            border: border,
            borderRadius: Math.min(style.card.radius, 8),
            padding: '8px 9px',
            minHeight: 60,
            color: fg,
            overflow: 'hidden',
          },
        },
        [
          h(
            'div',
            {
              key: 't',
              style: {
                fontFamily: style.title.family,
                fontWeight: style.title.weight,
                fontSize: Math.max(11, Math.min(14, style.title.size * 0.55)),
                lineHeight: 1.2,
                color: style.title.color === 'primary' ? color.primary : fg,
                textAlign: style.title.align,
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              },
            },
            '标题',
          ),
          h('div', {
            key: 'a',
            style: {
              height: 3,
              width: 26,
              background: color.primary,
              margin: '6px 0 5px',
              borderRadius: 9,
              opacity: style.accent === 'none' ? 0.5 : 1,
            },
          }),
          h('div', {
            key: 'l1',
            style: {
              height: 3,
              width: '86%',
              background: fg,
              opacity: 0.28,
              borderRadius: 9,
              marginBottom: 4,
            },
          }),
          h('div', {
            key: 'l2',
            style: {
              height: 3,
              width: '64%',
              background: fg,
              opacity: 0.18,
              borderRadius: 9,
            },
          }),
        ],
      )
    }

    function StyleCard(props) {
      var style = props.style
      var color = props.color
      var selected = props.selected
      return h(
        'button',
        {
          type: 'button',
          onClick: props.onClick,
          title: style.description,
          'aria-pressed': selected,
          style: {
            display: 'flex',
            flexDirection: 'column',
            gap: 7,
            textAlign: 'left',
            padding: 8,
            borderRadius: 10,
            border: selected
              ? '2px solid ' + color.primary
              : '1px solid rgba(127,127,127,.22)',
            background: selected
              ? 'rgba(127,127,127,.10)'
              : 'rgba(127,127,127,.03)',
            color: 'inherit',
            cursor: 'pointer',
            minWidth: 0,
            position: 'relative',
          },
        },
        [
          styleThumb(style, color),
          h(
            'div',
            {
              key: 'name',
              style: {
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 5,
                fontSize: 12,
                fontWeight: 600,
              },
            },
            [
              h('span', { key: 'n' }, style.name),
              selected
                ? h(
                    'span',
                    {
                      key: 'check',
                      style: { color: color.primary, fontSize: 13 },
                    },
                    '✓',
                  )
                : null,
            ],
          ),
          h(
            'div',
            {
              key: 'desc',
              style: {
                fontSize: 10.5,
                lineHeight: 1.45,
                opacity: 0.62,
                display: '-webkit-box',
                WebkitLineClamp: 2,
                WebkitBoxOrient: 'vertical',
                overflow: 'hidden',
              },
            },
            style.description,
          ),
        ],
      )
    }

    function AutoStyleCard(props) {
      var theme = props.theme
      var color = props.color
      var selected = props.selected
      var auto = theme.autoStyle
      return h(
        'button',
        {
          type: 'button',
          onClick: props.onClick,
          'aria-pressed': selected,
          style: {
            width: '100%',
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            textAlign: 'left',
            padding: '10px 12px',
            borderRadius: 10,
            border: selected
              ? '2px solid ' + color.primary
              : '1px solid rgba(127,127,127,.22)',
            background: selected
              ? 'rgba(127,127,127,.10)'
              : 'rgba(127,127,127,.03)',
            color: 'inherit',
            cursor: 'pointer',
          },
        },
        [
          h(
            'span',
            {
              key: 'icon',
              style: {
                width: 30,
                height: 30,
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                borderRadius: 8,
                background: color.primary + '1F',
                color: color.primary,
                fontSize: 18,
              },
            },
            '✦',
          ),
          h(
            'span',
            { key: 'text', style: { flex: 1, minWidth: 0 } },
            [
              h(
                'span',
                {
                  key: 'line',
                  style: {
                    display: 'flex',
                    alignItems: 'center',
                    gap: 7,
                    fontSize: 12.5,
                    fontWeight: 600,
                  },
                },
                [
                  h('span', { key: 'n' }, auto.name),
                  h(
                    'span',
                    {
                      key: 'badge',
                      style: {
                        fontSize: 9.5,
                        padding: '1px 6px',
                        borderRadius: 99,
                        background: color.primary + '24',
                        color: color.primary,
                      },
                    },
                    auto.badge,
                  ),
                ],
              ),
              h(
                'span',
                {
                  key: 'desc',
                  style: { display: 'block', fontSize: 10.5, opacity: 0.62, marginTop: 2 },
                },
                auto.description,
              ),
            ],
          ),
          selected
            ? h(
                'span',
                {
                  key: 'check',
                  style: { color: color.primary, fontSize: 14 },
                },
                '✓',
              )
            : null,
        ],
      )
    }

    function ColorDot(props) {
      var color = props.color
      var auto = props.auto
      var selected = props.selected
      var swatch = auto ? props.theme.autoColor.swatch : color.primary
      var name = auto ? props.theme.autoColor.name : color.name
      var sub = auto ? props.theme.autoColor.description : null
      return h(
        'button',
        {
          type: 'button',
          onClick: props.onClick,
          'aria-pressed': selected,
          title: name,
          style: {
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            gap: 5,
            border: 'none',
            background: 'transparent',
            color: 'inherit',
            cursor: 'pointer',
            padding: '2px 0',
            minWidth: 0,
          },
        },
        [
          h(
            'span',
            {
              key: 'dot',
              style: {
                width: 28,
                height: 28,
                borderRadius: '50%',
                background: swatch,
                boxShadow: selected
                  ? '0 0 0 3px rgba(201,94,87,.55), 0 0 0 5px rgba(201,94,87,.16)'
                  : '0 0 0 1px rgba(127,127,127,.28)',
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: '#fff',
                fontSize: 11,
              },
            },
            selected ? '✓' : '',
          ),
          h(
            'span',
            {
              key: 'name',
              style: {
                fontSize: 10.5,
                lineHeight: 1.25,
                whiteSpace: 'nowrap',
                opacity: selected ? 1 : 0.72,
                fontWeight: selected ? 600 : 400,
              },
            },
            name,
          ),
          sub
            ? h(
                'span',
                {
                  key: 'sub',
                  style: { fontSize: 9, opacity: 0.45, lineHeight: 1 },
                },
                sub,
              )
            : null,
        ],
      )
    }

    function SectionTitle(props) {
      return h(
        'div',
        {
          style: {
            display: 'flex',
            alignItems: 'baseline',
            justifyContent: 'space-between',
            gap: 8,
            margin: '4px 0 8px',
          },
        },
        [
          h(
            'div',
            { key: 't', style: { fontSize: 13, fontWeight: 700 } },
            props.children,
          ),
          props.extra
            ? h(
                'div',
                { key: 'e', style: { fontSize: 10.5, opacity: 0.55 } },
                props.extra,
              )
            : null,
        ],
      )
    }

    function Field(props) {
      return h(
        'label',
        { style: { display: 'grid', gap: 5, flex: '1 1 220px', minWidth: 0 } },
        [
          h(
            'span',
            {
              key: 'label',
              style: { fontSize: 11, opacity: 0.68, fontWeight: 600 },
            },
            props.label,
          ),
          props.children,
        ],
      )
    }

    function inputStyle() {
      return {
        width: '100%',
        boxSizing: 'border-box',
        padding: '8px 10px',
        borderRadius: 8,
        border: '1px solid rgba(127,127,127,.35)',
        background: 'rgba(127,127,127,.06)',
        color: 'inherit',
        fontSize: 13,
        outline: 'none',
      }
    }

    function buttonStyle(kind) {
      var primary = kind === 'primary'
      var danger = kind === 'danger'
      return {
        padding: '7px 13px',
        borderRadius: 8,
        border: primary
          ? '1px solid #C95E57'
          : danger
            ? '1px solid rgba(180,60,60,.55)'
            : '1px solid rgba(127,127,127,.32)',
        background: primary
          ? '#C95E57'
          : danger
            ? 'rgba(180,60,60,.10)'
            : 'rgba(127,127,127,.06)',
        color: primary ? '#fff' : 'inherit',
        fontSize: 12,
        fontWeight: primary ? 600 : 500,
        cursor: 'pointer',
        whiteSpace: 'nowrap',
      }
    }

    function Panel(_props) {
      var themeState = useState(null)
      var theme = themeState[0]
      var setTheme = themeState[1]
      var configState = useState(null)
      var config = configState[0]
      var setConfig = configState[1]
      var loadingState = useState(true)
      var loading = loadingState[0]
      var setLoading = loadingState[1]
      var errorState = useState('')
      var error = errorState[0]
      var setError = errorState[1]
      var noticeState = useState('')
      var notice = noticeState[0]
      var setNotice = noticeState[1]
      var showPasswordState = useState(false)
      var showPassword = showPasswordState[0]
      var setShowPassword = showPasswordState[1]
      var expandedState = useState(false)
      var expanded = expandedState[0]
      var setExpanded = expandedState[1]
      var previewTitleState = useState(PREVIEW_TITLE)
      var previewTitle = previewTitleState[0]
      var setPreviewTitle = previewTitleState[1]
      var resolvedState = useState(null)
      var resolved = resolvedState[0]
      var setResolved = resolvedState[1]
      var busyState = useState('')
      var busy = busyState[0]
      var setBusy = busyState[1]

      useEffect(function () {
        var alive = true
        Promise.all([request('/theme'), request('/config')])
          .then(function (values) {
            if (!alive) return
            var nextTheme = values[0]
            var nextConfig = values[1]
            setTheme(nextTheme)
            setConfig(nextConfig)
            setResolved({
              stylePreset: nextConfig.resolvedStyle,
              colorTheme: nextConfig.resolvedTheme,
              styleName: nextConfig.resolvedStyleName,
              colorName: nextConfig.resolvedThemeName,
              reason: nextConfig.resolveReason || '',
            })
            setError('')
          })
          .catch(function (err) {
            if (alive) setError(errorText(err))
          })
          .then(function () {
            if (alive) setLoading(false)
          })
        return function () {
          alive = false
        }
      }, [])

      useEffect(
        function () {
          if (!config) return undefined
          var timer = window.setInterval(function () {
            request('/config')
              .then(function (latest) {
                setConfig(function (prev) {
                  if (!prev) return prev
                  return Object.assign({}, prev, {
                    loggedIn: latest.loggedIn,
                    browserRunning: latest.browserRunning,
                    url: latest.url,
                    hasPassword: latest.hasPassword,
                  })
                })
              })
              .catch(function () {
                // 状态轮询失败不打断设置页；下一次再试。
              })
          }, 6000)
          return function () {
            window.clearInterval(timer)
          }
        },
        [Boolean(config)],
      )

      useEffect(
        function () {
          if (!theme || !config) return undefined
          if (config.stylePreset !== 'auto' && config.colorTheme !== 'auto') {
            var local = buildLocalTheme(
              theme,
              config.stylePreset,
              config.colorTheme,
            )
            if (local) {
              setResolved({
                stylePreset: local.style.id,
                colorTheme: local.color.id,
                styleName: local.style.name,
                colorName: local.color.name,
                reason:
                  '已手动选择' + local.style.name + ' + ' + local.color.name,
              })
            }
            return undefined
          }
          var timer = window.setTimeout(function () {
            request('/resolve', {
              method: 'POST',
              body: JSON.stringify({
                title: previewTitle,
                textPreview: PREVIEW_PARAGRAPHS.join('\n'),
              }),
            })
              .then(function (result) {
                setResolved({
                  stylePreset: result.resolvedStyle,
                  colorTheme: result.resolvedTheme,
                  styleName: result.resolvedStyleName,
                  colorName: result.resolvedThemeName,
                  reason: result.resolveReason || '',
                })
              })
              .catch(function (err) {
                setError(errorText(err))
              })
          }, 280)
          return function () {
            window.clearTimeout(timer)
          }
        },
        [
          theme,
          config && config.stylePreset,
          config && config.colorTheme,
          previewTitle,
        ],
      )

      var colors = useMemo(
        function () {
          if (!config || !theme) return null
          return (
            findColor(theme, config.colorTheme) ||
            findColor(theme, 'vermilion') ||
            theme.colors[0]
          )
        },
        [theme, config && config.colorTheme],
      )

      var previewTheme = useMemo(
        function () {
          if (!theme) return null
          var styleId =
            (resolved && resolved.stylePreset) ||
            (config && config.stylePreset) ||
            'minimal'
          var colorId =
            (resolved && resolved.colorTheme) ||
            (config && config.colorTheme) ||
            'vermilion'
          return buildLocalTheme(theme, styleId, colorId)
        },
        [theme, resolved],
      )

      if (loading) {
        return h(
          'div',
          { style: { padding: 18, fontSize: 12, opacity: 0.7 } },
          '正在加载秀米助手设置…',
        )
      }
      if (!theme || !config) {
        return h(
          'div',
          {
            style: {
              padding: 14,
              borderRadius: 10,
              border: '1px solid rgba(180,60,60,.35)',
              background: 'rgba(180,60,60,.08)',
              fontSize: 12,
              lineHeight: 1.6,
            },
          },
          [
            h('div', { key: 't', style: { fontWeight: 700, marginBottom: 4 } }, '设置页加载失败'),
            h('div', { key: 'e', style: { opacity: 0.8 } }, error || 'Host 未返回配置或主题表。'),
          ],
        )
      }

      function updateConfig(patch) {
        setConfig(function (prev) {
          return Object.assign({}, prev, patch)
        })
      }

      async function saveConfig() {
        setBusy('save')
        setError('')
        setNotice('')
        try {
          await request('/config', {
            method: 'POST',
            body: JSON.stringify({
              account: config.account,
              password: config.password,
              stylePreset: config.stylePreset,
              colorTheme: config.colorTheme,
            }),
          })
          updateConfig({ hasPassword: Boolean(config.password) })
          setNotice('已保存。账号与主题会写入用户目录的配置文件，刷新后仍会回填。')
        } catch (err) {
          setError(errorText(err))
        } finally {
          setBusy('')
        }
      }

      async function applySelection(stylePreset, colorTheme) {
        if (busy) return
        var next = Object.assign({}, config, { stylePreset: stylePreset, colorTheme: colorTheme })
        setConfig(next)
        setBusy('theme')
        setError('')
        setNotice('')
        try {
          await request('/config', {
            method: 'POST',
            body: JSON.stringify({
              account: next.account,
              password: next.password,
              stylePreset: stylePreset,
              colorTheme: colorTheme,
            }),
          })
          setNotice('主题已保存，刷新后仍会保持。')
        } catch (err) {
          setError(errorText(err))
        } finally {
          setBusy('')
        }
      }

      async function openBrowser() {
        setBusy('open')
        setError('')
        setNotice('')
        try {
          var result = await request('/open', { method: 'POST' })
          updateConfig({
            browserRunning: Boolean(result.running),
            loggedIn: Boolean(result.loggedIn),
            url: String(result.url || ''),
          })
          setNotice(result.running ? '已打开（或复用）可见浏览器窗口。' : '浏览器未打开。')
        } catch (err) {
          setError(errorText(err))
        } finally {
          setBusy('')
        }
      }

      async function login() {
        setBusy('login')
        setError('')
        setNotice('')
        try {
          var result = await request('/login', { method: 'POST' })
          updateConfig({
            loggedIn: Boolean(result.loggedIn),
            browserRunning: Boolean(result.browserRunning),
            url: String(result.url || ''),
          })
          if (result.captchaRequired) {
            setNotice(
              result.message ||
                '秀米要求验证码：请在弹出的浏览器窗口里手动完成，然后回来点「登录秀米」或刷新确认。',
            )
          } else if (result.ok === false) {
            setError(result.message || '登录失败，请检查账号与密码。')
          } else {
            setNotice(
              result.loggedIn
                ? '登录成功，已进入已登录状态。'
                : result.message || '登录未完成，请查看可见浏览器窗口。',
            )
          }
        } catch (err) {
          setError(errorText(err))
        } finally {
          setBusy('')
        }
      }

      async function logout() {
        setBusy('logout')
        setError('')
        setNotice('')
        try {
          var result = await request('/logout', {
            method: 'POST',
            body: JSON.stringify({ keepProfile: true, clearCredentials: true }),
          })
          updateConfig({
            loggedIn: Boolean(result.loggedIn),
            browserRunning: Boolean(result.browserRunning),
            url: String(result.url || ''),
            account: result.credentialsCleared ? '' : config.account,
            password: result.credentialsCleared ? '' : config.password,
            hasPassword: result.credentialsCleared ? false : config.hasPassword,
          })
          if (result.loggedIn) {
            setError(result.message || '页面仍显示已登录，请点「打开浏览器」刷新后再试。')
          } else {
            setNotice(result.message || '已退出秀米账号，并清空本机保存的账号与密码。')
          }
        } catch (err) {
          setError(errorText(err))
        } finally {
          setBusy('')
        }
      }

      function restoreAuto() {
        applySelection('auto', 'auto')
      }

      var styleGroups = theme.groups.map(function (group) {
        return {
          id: group.id,
          name: group.name,
          items: theme.styles.filter(function (style) {
            return style.group === group.id
          }),
        }
      })

      var selectedStyleName =
        findStyle(theme, config.stylePreset) &&
        findStyle(theme, config.stylePreset).name
      var summary = styleGroups
        .map(function (group) {
          return group.name.replace('组', '') + ' ' + group.items.length
        })
        .join(' · ')

      return h(
        'div',
        { style: { display: 'grid', gap: 14, padding: '2px 0 10px' } },
        [
          h(
            'div',
            { key: 'head', style: { display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' } },
            [
              h(
                'div',
                { key: 'left' },
                [
                  h(
                    'div',
                    { key: 'title', style: { fontSize: 15, fontWeight: 700, marginBottom: 3 } },
                    '秀米助手',
                  ),
                  h(
                    'div',
                    { key: 'sub', style: { fontSize: 11, lineHeight: 1.6, opacity: 0.62 } },
                    '账号密码只保存在本机用户目录的配置文件中；密码不会写入插件源码或日志。',
                  ),
                ],
              ),
              h(
                'div',
                { key: 'status', style: { display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap', fontSize: 11, opacity: 0.75 } },
                [
                  h('span', { key: 'b' }, '浏览器：' + (config.browserRunning ? '已打开' : '未打开')),
                  h('span', { key: 'l' }, '登录：' + (config.loggedIn ? '已登录' : '未登录')),
                  config.url
                    ? h('span', { key: 'u', style: { maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, '页面：' + config.url)
                    : null,
                ],
              ),
            ],
          ),

          h(
            'div',
            { key: 'account', style: { display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' } },
            [
              h(Field, { key: 'a', label: '秀米账号（手机号或邮箱）' }, h('input', {
                value: config.account || '',
                onChange: function (event) {
                  updateConfig({ account: event.target.value })
                },
                placeholder: '请输入秀米账号',
                autoComplete: 'username',
                spellCheck: false,
                style: inputStyle(),
              })),
              h(Field, { key: 'p', label: '密码' }, h('div', { style: { display: 'flex', gap: 6 } }, [
                h('input', {
                  key: 'input',
                  value: config.password || '',
                  type: showPassword ? 'text' : 'password',
                  onChange: function (event) {
                    updateConfig({ password: event.target.value })
                  },
                  placeholder: config.hasPassword ? '已保存密码（显示为圆点）' : '请输入密码',
                  autoComplete: 'current-password',
                  spellCheck: false,
                  style: inputStyle(),
                }),
                h('button', {
                  key: 'toggle',
                  type: 'button',
                  onClick: function () {
                    setShowPassword(!showPassword)
                  },
                  style: buttonStyle(),
                }, showPassword ? '隐藏' : '显示'),
              ])),
            ],
          ),

          h(
            'div',
            { key: 'actions', style: { display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' } },
            [
              h('button', { key: 'save', type: 'button', disabled: Boolean(busy), onClick: saveConfig, style: buttonStyle('primary') }, busy === 'save' ? '保存中…' : '保存账号与主题'),
              h('button', { key: 'open', type: 'button', disabled: Boolean(busy), onClick: openBrowser, style: buttonStyle() }, busy === 'open' ? '打开中…' : '打开浏览器'),
              h('button', {
                key: 'login',
                type: 'button',
                disabled: Boolean(busy) || !config.account || !config.password,
                onClick: login,
                style: buttonStyle('primary'),
              }, busy === 'login' ? '登录中…' : '登录秀米'),
              h('button', {
                key: 'logout',
                type: 'button',
                disabled: Boolean(busy) || !config.browserRunning,
                onClick: logout,
                style: buttonStyle(),
              }, busy === 'logout' ? '退出中…' : '退出账号'),
              h('button', { key: 'auto', type: 'button', disabled: Boolean(busy), onClick: restoreAuto, style: buttonStyle() }, '恢复自动'),
            ],
          ),

          error
            ? h('div', { key: 'err', style: { padding: '9px 11px', borderRadius: 8, border: '1px solid rgba(180,60,60,.35)', background: 'rgba(180,60,60,.08)', fontSize: 11.5, lineHeight: 1.5 } }, error)
            : null,
          notice
            ? h('div', { key: 'notice', style: { padding: '9px 11px', borderRadius: 8, border: '1px solid rgba(60,150,100,.3)', background: 'rgba(60,150,100,.08)', fontSize: 11.5, lineHeight: 1.5 } }, notice)
            : null,

          h('div', { key: 'styleSection', style: { borderTop: '1px solid rgba(127,127,127,.16)', paddingTop: 14 } }, [
            h(SectionTitle, { key: 'st', extra: '当前：' + (selectedStyleName || '智能风格') }, '风格'),
            h(AutoStyleCard, {
              key: 'auto',
              theme: theme,
              color: colors,
              selected: config.stylePreset === 'auto',
              onClick: function () {
                applySelection('auto', config.colorTheme)
              },
            }),
            !expanded
              ? h('div', { key: 'summary', style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, marginTop: 10, fontSize: 11, opacity: 0.7 } }, [
                h('span', { key: 's' }, '共 13 种风格 · ' + summary),
                h('button', {
                  key: 'expand',
                  type: 'button',
                  onClick: function () {
                    setExpanded(true)
                  },
                  style: buttonStyle(),
                }, '展开全部风格'),
              ])
              : h('div', { key: 'all', style: { display: 'grid', gap: 10, marginTop: 10 } }, [
                styleGroups.map(function (group) {
                  return h('div', { key: group.id }, [
                    h('div', { key: 'h', style: { fontSize: 10.5, fontWeight: 600, opacity: 0.55, margin: '4px 0 6px' } }, group.name + ' · ' + group.items.length),
                    h('div', { key: 'g', style: { display: 'grid', gridTemplateColumns: 'repeat(4,minmax(0,1fr))', gap: 8 } }, group.items.map(function (style) {
                      return h(StyleCard, {
                        key: style.id,
                        style: style,
                        color: colors,
                        selected: config.stylePreset === style.id,
                        onClick: function () {
                          applySelection(style.id, config.colorTheme)
                        },
                      })
                    })),
                  ])
                }),
                h('div', { key: 'collapse', style: { display: 'flex', justifyContent: 'flex-end' } }, h('button', {
                  type: 'button',
                  onClick: function () {
                    setExpanded(false)
                  },
                  style: buttonStyle(),
                }, '收起风格')),
              ]),
          ]),

          h('div', { key: 'colorSection', style: { borderTop: '1px solid rgba(127,127,127,.16)', paddingTop: 14 } }, [
            h(SectionTitle, { key: 'ct', extra: '当前：' + (colors ? colors.name : '自动') }, '配色'),
            h('div', { key: 'grid', style: { display: 'grid', gridTemplateColumns: 'repeat(6,minmax(0,1fr))', gap: '10px 6px' } }, [
              h(ColorDot, {
                key: 'auto',
                color: null,
                auto: true,
                theme: theme,
                selected: config.colorTheme === 'auto',
                onClick: function () {
                  applySelection(config.stylePreset, 'auto')
                },
              }),
              theme.colors.map(function (color) {
                return h(ColorDot, {
                  key: color.id,
                  color: color,
                  auto: false,
                  theme: theme,
                  selected: config.colorTheme === color.id,
                  onClick: function () {
                    applySelection(config.stylePreset, color.id)
                  },
                })
              }),
            ]),
          ]),

          h('div', { key: 'previewSection', style: { borderTop: '1px solid rgba(127,127,127,.16)', paddingTop: 14 } }, [
            h(SectionTitle, { key: 'pt', extra: '实时预览' }, '预览'),
            h('div', { key: 'inputrow', style: { display: 'flex', gap: 8, alignItems: 'center', marginBottom: 9, flexWrap: 'wrap' } }, [
              h('span', { key: 'label', style: { fontSize: 10.5, opacity: 0.6, whiteSpace: 'nowrap' } }, '自动匹配预览标题'),
              h('input', {
                key: 'title',
                value: previewTitle,
                onChange: function (event) {
                  setPreviewTitle(event.target.value)
                },
                placeholder: '试试输入：年度复盘',
                style: Object.assign({}, inputStyle(), { flex: '1 1 180px', minWidth: 140, width: 'auto' }),
              }),
            ]),
            resolved
              ? h('div', { key: 'resolved', style: { fontSize: 11, lineHeight: 1.6, marginBottom: 10, padding: '7px 9px', borderRadius: 7, background: 'rgba(127,127,127,.06)' } }, [
                h('div', { key: 'line', style: { fontWeight: 600 } }, '本次将使用：' + (resolved.styleName || '') + ' + ' + (resolved.colorName || '')),
                h('div', { key: 'reason', style: { opacity: 0.66, marginTop: 2 } }, resolved.reason || ''),
              ])
              : null,
            h('div', { key: 'preview', style: { maxWidth: 520 } }, h(Preview, { theme: previewTheme, title: previewTitle })),
          ]),
        ],
      )
    }

    function apply(ctx) {
      if (!ctx || !ctx.slots || typeof ctx.slots.inject !== 'function') return
      ctx.slots.inject('settings.section', function () {
        return ctx.slots.register(
          {
            name: 'settings.section',
            id: 'xiumi',
            order: 105,
            label: function () {
              return '秀米助手'
            },
          },
          Panel,
        )
      })
    }

    module.exports = { apply: apply, inject: inject }
    return module.exports
  },
})
