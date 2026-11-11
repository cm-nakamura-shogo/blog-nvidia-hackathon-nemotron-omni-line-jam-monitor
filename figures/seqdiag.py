"""matplotlib で簡単なシーケンス図を描く。

steps の要素:
  ('msg', src, dst, label)        実線の矢印（要求）
  ('reply', src, dst, label)      破線の矢印（応答）
  ('note', who, text)             参加者の上にメモ（who は名前か (左, 右) のタプル）
  ('group', label, [steps...])    枠で囲む（alt / loop など）
  ('gap',)                        少し間を空ける
"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle

plt.rcParams['font.family'] = ['Hiragino Sans', 'sans-serif']

ROW = 1.0
C_LINE = '#4a5568'
C_REQ = '#2b6cb0'
C_REP = '#718096'
C_NOTE_BG = '#fefcbf'
C_NOTE_EC = '#b7791f'
C_GROUP = '#c05621'


def _count_rows(steps):
    n = 0
    for s in steps:
        if s[0] == 'group':
            n += 0.75 + _count_rows(s[2]) + 0.4
        elif s[0] == 'gap':
            n += 0.5
        elif s[0] == 'note':
            n += 1.0 + 0.45 * (s[2].count('\n'))
        else:
            n += 1
    return n


def draw(participants, steps, out, title=None, col_width=3.4):
    xs = {p: i * col_width for i, p in enumerate(participants)}
    total = _count_rows(steps) + 1.5
    width = (len(participants) - 1) * col_width
    fig_h = 1.2 + total * 0.52
    fig, ax = plt.subplots(figsize=(max(7.5, width * 0.95 + 2.6), fig_h))
    ax.set_xlim(-col_width * 0.62, width + col_width * 0.62)
    ax.set_ylim(-total - 0.6, 1.6 if not title else 2.4)
    ax.axis('off')
    if title:
        ax.text(width / 2, 2.0, title, ha='center', va='center', fontsize=13, fontweight='bold', color='#1a202c')
    for p, x in xs.items():
        ax.add_patch(FancyBboxPatch((x - col_width * 0.42, 0.25), col_width * 0.84, 1.0,
                                    boxstyle='round,pad=0.02,rounding_size=0.15', fc='#edf2f7', ec=C_LINE, lw=1.4))
        ax.text(x, 0.75, p, ha='center', va='center', fontsize=10.5, color='#1a202c')
        ax.plot([x, x], [0.25, -total], color='#a0aec0', lw=1.1, ls=(0, (4, 3)), zorder=0)

    def render(steps, y, depth):
        for s in steps:
            kind = s[0]
            if kind in ('msg', 'reply'):
                _, a, b, label = s
                y -= ROW
                x1, x2 = xs[a], xs[b]
                color = C_REQ if kind == 'msg' else C_REP
                ls = '-' if kind == 'msg' else (0, (5, 3))
                if a == b:
                    ax.annotate('', xy=(x1 + 0.08, y - 0.25), xytext=(x1 + 0.08, y + 0.2),
                                arrowprops=dict(arrowstyle='-|>', color=color, lw=1.4, ls=ls,
                                                connectionstyle='arc3,rad=-1.4'))
                    ax.text(x1 + 0.55, y, label, ha='left', va='center', fontsize=9, color='#1a202c')
                else:
                    ax.annotate('', xy=(x2, y), xytext=(x1, y),
                                arrowprops=dict(arrowstyle='-|>', color=color, lw=1.5, ls=ls,
                                                shrinkA=0, shrinkB=0))
                    ax.text((x1 + x2) / 2, y + 0.12, label, ha='center', va='bottom', fontsize=9, color='#1a202c')
            elif kind == 'note':
                _, who, text = s
                lines = text.count('\n') + 1
                h = 0.62 + 0.45 * (lines - 1)
                y -= 0.35
                if isinstance(who, tuple):
                    left, right = xs[who[0]] - col_width * 0.38, xs[who[1]] + col_width * 0.38
                else:
                    left, right = xs[who] - col_width * 0.45, xs[who] + col_width * 0.45
                ax.add_patch(FancyBboxPatch((left, y - h), right - left, h,
                                            boxstyle='round,pad=0.02,rounding_size=0.08',
                                            fc=C_NOTE_BG, ec=C_NOTE_EC, lw=1.1, zorder=3))
                ax.text((left + right) / 2, y - h / 2, text, ha='center', va='center', fontsize=8.8,
                        color='#1a202c', zorder=4, linespacing=1.35)
                y -= h - 0.35 + 0.65
            elif kind == 'group':
                _, label, inner = s
                top = y - 0.25
                y -= 0.75
                y = render(inner, y, depth + 1)
                bottom = y - 0.4
                y = bottom
                left = -col_width * 0.55 + depth * 0.12
                right = width + col_width * 0.55 - depth * 0.12
                ax.add_patch(Rectangle((left, bottom), right - left, top - bottom, fill=False,
                                       ec=C_GROUP, lw=1.3, zorder=1))
                ax.add_patch(Rectangle((left, top - 0.55), min(6.2, len(label) * 0.22 + 0.6), 0.55,
                                       fc=C_GROUP, ec=C_GROUP, zorder=2))
                ax.text(left + 0.15, top - 0.28, label, ha='left', va='center', fontsize=8.8,
                        color='white', zorder=3)
            elif kind == 'gap':
                y -= 0.5
        return y

    render(steps, 0, 0)
    fig.savefig(out, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
