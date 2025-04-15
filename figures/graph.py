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
scenario_line2 = [
    '(500 GB)',
    '(Standard → Archive, 250 GB)', # EFS Size Changed
    '(Standard → Deep Archive, 1 TB)'
]
# Combine scenario lines for x-axis labels
scenario_details = [f"{s}\n{line1}\n{line2}" for s, line1, line2 in zip(s_services, scenario_line1, scenario_line2)]


# Costs updated for EFS 250GB scenario (estimated)
b_costs = np.array([50.00, 76.80, 23.00]) # EFS Before Cost updated
a_costs = np.array([40.00, 8.37, 0.99])  # EFS After Cost updated
savings = b_costs - a_costs # Calculate savings

num_scenarios = len(s_services)
index = np.arange(num_scenarios)
bar_width = 0.6 # Width of the bars for stacked bar chart

# --- Graph: Vertical Stacked Bar Chart (No Broken Axis) ---
fig, ax = plt.subplots(figsize=(8, 6))

# Define colors (emphasizing savings)
saving_color = 'dodgerblue'
after_cost_color = 'lightgrey'

# Plot vertical stacked bars: After Cost (bottom), Savings (top)
# zorder ensures grid lines are behind bars
rects1 = ax.bar(index, a_costs, bar_width, label='After Cost', color=after_cost_color, zorder=3)
rects2 = ax.bar(index, savings, bar_width, bottom=a_costs, label='Savings', color=saving_color, hatch='/', zorder=3)

# --- Add text labels on top of bars ---
def add_labels_on_top(rects_before, costs_before, costs_after, savings_val):
    """Attach a text label above each bar indicating Before Cost, After Cost, Savings Amount (bolded), and % Saved."""
    for i, rect in enumerate(rects_before):
        height = costs_before[i] # Use index for costs_before
        after_cost = costs_after[i] # Get the corresponding after cost
        saving_amount = savings_val[i] # Get the corresponding saving amount
        percentage_saved = (saving_amount / height * 100) if height > 0 else 0

        # 1. Add text label slightly above the bar top
        label_text = rf'$\${height:.2f} \rightarrow \${after_cost:.2f}$' + \
                     '\n' + \
                     rf'$(\mathbf{{\${saving_amount:.2f}\ \mathrm{{Saved}}}}, {percentage_saved:.1f}\%)$'
        ax.annotate(label_text,
                    xy=(rect.get_x() + rect.get_width() / 2, height),
                    xytext=(0, 5),  # 5 points vertical offset
                    textcoords="offset points",
                    ha='center', va='bottom', fontsize=8, color='black', zorder=4)

        # 2. Add arrow pointing down from Before Cost to After Cost level
        # Position arrow slightly to the right of the bar
        arrow_x = rect.get_x() + rect.get_width() * 1.1 # X position to the right
        # Arrow starts slightly above the bar top and ends slightly above the after_cost level
        # Avoid arrow starting/ending exactly on the bar line for visibility
        if height > after_cost: # Only draw arrow if there is a saving
            ax.annotate('', # No text for the arrow annotation
                        xy=(arrow_x, after_cost + (height-after_cost)*0.05), # Arrow end point (slightly above after_cost)
                        xytext=(arrow_x, height * 1.02), # Arrow start point (slightly above bar top)
                        arrowprops=dict(arrowstyle="->", color='red', lw=1.5), # Arrow style
                        zorder=3 # Ensure arrow is behind text label
                       )

# Use b_costs (total height) for positioning the top labels
# Need to plot invisible bars based on the actual `b_costs`
invisible_bars = ax.bar(index, b_costs, bar_width, alpha=0) # Plot invisible bars correctly
add_labels_on_top(invisible_bars, b_costs, a_costs, savings) # Pass after_costs (a_costs) to the function

# Customize plot
ax.set_xticks(index)
# Use combined scenario details for x-labels
ax.set_xticklabels(scenario_details, fontsize=9, ha='center')
ax.set_ylabel('Monthly Cost (USD)', fontsize=10)
# ax.set_title('AWS Service Cost Optimization', fontsize=14) # Title removed as per previous edit
ax.legend(fontsize=10, loc='upper right')
ax.yaxis.grid(True, linestyle='--', alpha=0.6, zorder=1)

# Remove spines (borders)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
ax.spines['left'].set_visible(True)
ax.spines['bottom'].set_visible(True)

# Adjust y-axis limits to prevent cutoff and give padding
max_cost = b_costs.max()
ax.set_ylim(0, max_cost * 1.25) # Adjust y-limit based on new max_cost

# Remove scenario descriptions below the plot
# ... (commented section remains commented) ...

# Adjust layout to prevent label overlap if necessary
fig.tight_layout()

# --- Show the plot ---
plt.show()

# --- Optional Saving ---
# fig.savefig('aws_service_cost_optimization_efs250gb.png', dpi=300, bbox_inches='tight') # Updated filename suggestion
