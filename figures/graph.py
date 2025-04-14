#!/usr/bin/env python
import matplotlib.pyplot as plt
import numpy as np

# 주석 처리 또는 제거된 한글 폰트 설정
# plt.rcParams['font.family'] = 'AppleGothic'
# plt.rcParams['axes.unicode_minus'] = False # Keep this if needed for minus sign

# --- Data Definition ---
s_services = ['EBS', 'EFS', 'RDS', 'S3']
# Scenario details split into two lines for annotation
scenario_line1 = [
    'gp3 ← gp2 Migration',          # Line 1: Action
    'Lifecycle Management',         # Line 1: Action
    'Instance Downsizing',        # Line 1: Action
    'Lifecycle Policy'            # Line 1: Action
]
scenario_line2 = [
    '(500 GB)',                     # Line 2: Detail/Context
    '(Archive ← Standard, 1 TB)',   # Swapped text around left arrow
    '(m5.large ← m5.xlarge)',       # Swapped text around left arrow
    '(Deep Archive ← Standard, 1 TB)' # Swapped text around left arrow
]

b_costs = np.array([50.00, 307.20, 292.00, 23.00]) # Before Costs
a_costs = np.array([40.00, 33.48, 146.00, 0.99])  # After Costs

num_scenarios = len(s_services)
index = np.arange(num_scenarios)

# --- Graph: Refined Dumbbell Plot ---
fig, ax = plt.subplots(figsize=(8, 6))

for i in range(num_scenarios):
    # Plot horizontal line (dumbbell bar)
    ax.plot([a_costs[i], b_costs[i]], [index[i], index[i]], linewidth=2, color='grey', alpha=0.7, zorder=1)
    # Plot points (before and after)
    ax.scatter([a_costs[i]], [index[i]], color='lightcoral', s=80, label='After Cost' if i == 0 else "", zorder=2)
    ax.scatter([b_costs[i]], [index[i]], color='skyblue', s=80, label='Before Cost' if i == 0 else "", zorder=2)

    # --- Add text labels for costs ---
    if s_services[i] == 'EBS': # Special handling for EBS label positioning
        horizontal_offset = 3
        ax.text(a_costs[i] - horizontal_offset, index[i], f'${a_costs[i]:.2f}', ha='right', va='center', fontsize=9, color='dimgray', zorder=3)
        ax.text(b_costs[i] + horizontal_offset, index[i], f'${b_costs[i]:.2f}', ha='left', va='center', fontsize=9, color='dimgray', zorder=3)
    else: # Default positioning for other services
        vertical_offset = 0.1
        ax.text(a_costs[i], index[i] + vertical_offset, f'${a_costs[i]:.2f}', ha='center', va='bottom', fontsize=9, color='dimgray', zorder=3)
        ax.text(b_costs[i], index[i] + vertical_offset, f'${b_costs[i]:.2f}', ha='center', va='bottom', fontsize=9, color='dimgray', zorder=3)

    # --- Add scenario detail text on the dumbbell line ---
    mid_point_x = (a_costs[i] + b_costs[i]) / 2
    text_y_offset = 0.05 # Base vertical offset
    va_align = 'bottom' # Default alignment (above line)

    if s_services[i] == 'S3':
        va_align = 'top' # Place below line for S3
        text_y_offset *= -1 # Reverse offset direction

    # Combine two lines for the detail text, increase font size
    detail_text = f"{scenario_line1[i]}\n{scenario_line2[i]}" 
    ax.text(mid_point_x, index[i] + text_y_offset, detail_text,
            ha='center', va=va_align, fontsize=8, color='black', # Fontsize increased to 8
            bbox=dict(boxstyle='round,pad=0.2', fc='white', ec='none', alpha=0.7), zorder=4)

# Customize plot
ax.set_yticks(index)
ax.set_yticklabels(s_services, fontsize=9) # Use service names for y-labels
ax.set_xlabel('Monthly Cost (USD)', fontsize=10)
ax.set_title('AWS Storage Cost Optimization', fontsize=14) # Emphasize Storage
ax.legend(fontsize=10, loc='upper right') # Changed legend location to upper right
ax.xaxis.grid(True, linestyle='--', alpha=0.6)

# Remove spines (borders) - keep left for y-axis context
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
ax.spines['left'].set_visible(True) # Keep left spine
ax.spines['bottom'].set_visible(True)

# Adjust x-axis limits to prevent cutoff and give padding
min_cost = min(a_costs.min(), b_costs.min())
max_cost = max(a_costs.max(), b_costs.max())
ax.set_xlim(min_cost - 15, max_cost + 15) # Adjusted limits slightly more padding

# Remove scenario descriptions below the plot (now added on the lines)
# scenario_text_below = "\n".join([f"{i+1}. {detail}" for i, detail in enumerate(scenario_details)])
# fig.text(0.5, -0.05, scenario_text_below, ha='center', va='top', fontsize=8, wrap=True)

# Remove layout adjustment for bottom text
# plt.subplots_adjust(bottom=0.25)
fig.tight_layout() # Use standard tight_layout

# --- Show the plot ---
plt.show()

# --- Optional Saving ---
# fig.savefig('aws_storage_cost_optimization_dumbbell_final_annotated.png', dpi=300, bbox_inches='tight')
