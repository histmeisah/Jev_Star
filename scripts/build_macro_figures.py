"""Generate the five-configuration architecture and recorded Lv7 outcomes."""

import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'paper/figures'
plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 9,
                     'pdf.fonttype': 42, 'ps.fonttype': 42, 'axes.spines.top': False,
                     'axes.spines.right': False, 'savefig.facecolor': 'white'})
BLUE, PURPLE, GRAY, AMBER = '#356B9A', '#77568E', '#A4ADB5', '#D6A44A'


def architecture():
    fig, ax = plt.subplots(figsize=(7.0, 3.7))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 6)
    ax.axis('off')
    boxes = [(0.1, 'Structured\nobservation'), (2.7, 'Executor\ncandidates'),
             (5.3, 'JEV or\nuniform random'), (7.9, 'Validate\nand execute')]
    for x, label in boxes:
        ax.add_patch(FancyBboxPatch((x, 4.65), 2.0, .85, boxstyle='round,pad=.05',
                                   facecolor='#F2F5F8', edgecolor=BLUE, linewidth=.9))
        ax.text(x + 1, 5.075, label, ha='center', va='center', fontsize=9)
    for left, right in zip(boxes, boxes[1:]):
        ax.add_patch(FancyArrowPatch((left[0]+2.08, 5.075), (right[0]-.08, 5.075),
                                    arrowstyle='-|>', mutation_scale=10, color=BLUE, linewidth=1))
    ax.text(5, 5.85, 'SC2 advances while model requests run in the background',
            ha='center', va='center', fontsize=9, color=BLUE)
    ax.text(5, 4.13, 'Optional Astra plan: candidate rules in constrained mode; context in advisory mode',
            ha='center', va='center', fontsize=8.5, color=PURPLE)
    headers = ['Configuration', 'Astra plan', 'Plan masks', 'Selector']
    xs = [.15, 5.25, 7.05, 8.8]
    for x, h in zip(xs, headers):
        ax.text(x, 3.45, h, weight='bold', va='center')
    rows = [('Pure random', 'Absent', 'None', 'Random'),
            ('JEV-only', 'Absent', 'None', 'JEV'),
            ('Astra constrained + random', 'Present', 'Applied', 'Random'),
            ('Astra constrained + JEV', 'Present', 'Applied', 'JEV'),
            ('Astra advisory + JEV', 'Present', 'None', 'JEV')]
    ax.plot([.1, 9.95], [3.15, 3.15], color='#CDD3D8', linewidth=.8)
    for i, row in enumerate(rows):
        y = 2.78 - i * .48
        for x, value in zip(xs, row):
            ax.text(x, y, value, va='center', fontsize=9)
    ax.text(.15, .2, 'Every configuration retains local availability and execution rules; WAIT is a candidate.',
            color='#555555', fontsize=8.2)
    fig.subplots_adjust(left=.01, right=.99, bottom=.01, top=.99)
    fig.savefig(OUTPUT/'architecture.pdf')
    previews = ROOT / '.tools/figure-previews'
    previews.mkdir(parents=True, exist_ok=True)
    fig.savefig(previews/'architecture.png', dpi=150)
    plt.close(fig)


def outcomes():
    manifest = json.loads((ROOT/'paper/data/macro_realtime/manifest.json').read_text(encoding='utf-8'))
    rows = manifest['configurations']
    fig, ax = plt.subplots(figsize=(7.0, 3.3))
    for i, row in enumerate(rows):
        if not row['evaluated_at_lv7']:
            ax.text(.08, i, 'Not evaluated at Lv7', va='center', color='#666666', fontsize=9)
            continue
        left = 0
        for key, color, label in [('wins', BLUE, 'Win'), ('losses', GRAY, 'Loss'), ('time_limits', AMBER, 'Time limit')]:
            value = row[key]
            if value:
                ax.barh(i, value, left=left, height=.55, color=color, edgecolor='white', linewidth=.8)
                ax.text(left+value/2, i, str(value), ha='center', va='center',
                        color='white' if key=='wins' else '#202020', fontsize=10)
            left += value
        if row['human_adjudicated_games']:
            ax.barh(i, 1, left=9, height=.55, facecolor='none', edgecolor='#555555', hatch='////', linewidth=.5)
    ax.set_yticks(range(len(rows)), [row['label'] for row in rows])
    ax.invert_yaxis()
    ax.set_xlim(0, 10)
    ax.set_xticks(range(0,11,2))
    ax.set_xlabel('Games (ten seeds per evaluated configuration)')
    ax.spines[['left','right','top']].set_visible(False)
    ax.tick_params(axis='y', length=0)
    ax.xaxis.grid(True, color='#E8E8E8', linewidth=.6)
    ax.set_axisbelow(True)
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=BLUE,label='Win'),Patch(color=GRAY,label='Loss'),
                       Patch(color=AMBER,label='Time limit')],loc='upper center',
              bbox_to_anchor=(.5,1.22),ncol=3,frameon=False,fontsize=8.5)
    fig.subplots_adjust(left=.34,right=.97,bottom=.24,top=.85)
    fig.text(.02,.035,'Hatched loss: human adjudication without a replay. Fixed-seed development samples.',
             fontsize=8,color='#555555')
    fig.savefig(OUTPUT/'macro_realtime_outcomes.pdf')
    fig.savefig(ROOT/'.tools/figure-previews/macro_realtime_outcomes.png',dpi=150)
    plt.close(fig)


if __name__ == '__main__':
    OUTPUT.mkdir(parents=True, exist_ok=True)
    architecture()
    outcomes()
    print('Built architecture.pdf and macro_realtime_outcomes.pdf')
