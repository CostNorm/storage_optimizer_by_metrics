#!/usr/bin/env python
import matplotlib.pyplot as plt
import numpy as np

# 주석 처리 또는 제거된 한글 폰트 설정
# plt.rcParams['font.family'] = 'AppleGothic'
# plt.rcParams['axes.unicode_minus'] = False # Keep this if needed for minus sign

# --- Data Definition (RDS excluded, EFS modified to 250GB) ---
s_services = ['EBS', 'EFS', 'S3']
scenario_line1 = [
    'gp2 → gp3 Migration',
    'Lifecycle Management', # EFS Scenario
    'Lifecycle Policy'
]
# Scenario details for size - no longer used for x-labels
# scenario_line2 = [
#     '(500 GB)',
#     '(Standard → Archive, 250 GB)', # EFS Size Changed
#     '(Standard → Deep Archive, 1 TB)'
# ]
# Combine scenario lines for x-axis labels (without size)
scenario_details = [f"{s}\n{line1}" for s, line1 in zip(s_services, scenario_line1)]


# Costs updated for EFS 250GB scenario (estimated)
b_costs = np.array([50.00, 76.80, 23.00]) # EFS Before Cost updated
a_costs = np.array([40.00, 8.37, 0.99])  # EFS After Cost updated
savings = b_costs - a_costs # Calculate savings

# --- Calculate percentages ---
# Handle potential division by zero, though not expected here
a_costs_percent = np.divide(a_costs, b_costs, out=np.zeros_like(a_costs), where=b_costs!=0) * 100
savings_percent = np.divide(savings, b_costs, out=np.zeros_like(savings), where=b_costs!=0) * 100

num_scenarios = len(s_services)
index = np.arange(num_scenarios)
bar_width = 0.6 # Width of the bars for stacked bar chart

# --- Graph: Vertical Stacked Bar Chart (Percentage View) ---
fig, ax = plt.subplots(figsize=(8, 6))

# Define colors (emphasizing savings)
saving_color = 'dodgerblue'
after_cost_color = 'lightgrey'

# Plot vertical stacked bars using percentages: After Cost % (bottom), Savings % (top)
rects1 = ax.bar(index, a_costs_percent, bar_width, label='After Cost %', color=after_cost_color, zorder=3)
rects2 = ax.bar(index, savings_percent, bar_width, bottom=a_costs_percent, label='Savings %', color=saving_color, hatch='/', zorder=3)

# --- Add text labels on top of bars (Percentage View - Simplified) ---
def add_labels_on_top_percent(rects_list, savings_p, after_p):
    """Attach a text label above each 100% bar indicating Saved %."""
    for i, rect in enumerate(rects_list):
        saved_percentage = savings_p[i]
        after_percentage = after_p[i]

        # Position label slightly above the 100% bar top (lowered offset)
        # Format: Saved: XX.X%
        label_text = rf'$\mathbf{{{saved_percentage:.1f}\%\ \mathrm{{Saved}}}}$'
        ax.annotate(label_text,
                    xy=(rect.get_x() + rect.get_width() / 2, 100), # Label anchored at 100%
                    xytext=(0, 2),  # Lowered vertical offset (2 points)
                    textcoords="offset points",
                    ha='center', va='bottom', fontsize=9, color='black', zorder=4) # Slightly larger font

        # Add arrow pointing down from 100% level to After Cost % level (lowered start)
        arrow_x = rect.get_x() + rect.get_width() * 1.1 # X position to the right
        if saved_percentage > 0: # Only draw arrow if there is a saving
            ax.annotate('', # No text for the arrow annotation
                        xy=(arrow_x, after_percentage + (100-after_percentage)*0.05), # Arrow end point
                        xytext=(arrow_x, 100), # Arrow start point lowered to 100%
                        arrowprops=dict(arrowstyle="->", color='red', lw=1.5),
                        zorder=3
                       )

# Use invisible bars reaching 100% for positioning the top labels and arrows
invisible_bars_100 = ax.bar(index, 100, bar_width, alpha=0)
add_labels_on_top_percent(invisible_bars_100, savings_percent, a_costs_percent) # Removed b_costs from call

# Customize plot
ax.set_xticks(index)
ax.set_xticklabels(scenario_details, fontsize=9, ha='center')
ax.set_ylabel('Percentage of Original Cost (%)', fontsize=10) # Updated Y label
# ax.set_title('AWS Service Cost Optimization (% View)', fontsize=14) # Title removed
ax.legend(fontsize=10, loc='upper right')
ax.yaxis.grid(True, linestyle='--', alpha=0.6, zorder=1)

# Remove spines (borders)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
ax.spines['left'].set_visible(True)
ax.spines['bottom'].set_visible(True)

# Adjust y-axis limits for percentage view (lowered top limit)
ax.set_ylim(0, 120) # Set Y limit slightly lower to avoid legend overlap

# Remove scenario descriptions below the plot
# ... (commented section remains commented) ...

# Adjust layout to prevent label overlap if necessary
fig.tight_layout()

# --- Show the plot ---
plt.show()

# --- Optional Saving ---
# fig.savefig('aws_service_cost_optimization_percent_view.png', dpi=300, bbox_inches='tight')
