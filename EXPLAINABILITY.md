# Zomato Wallet Order Agent Explainability

## Agent Decision Reasoning
The agent uses a schedule-driven approach to determine the optimal time to order food during the day. It decides which restaurant and items to choose by evaluating Zomato MCP search results against the user's pre-configured preferences and current wallet balance.

## Data Inputs
The primary data source for schedule monitoring is the Google Calendar MCP, which provides upcoming meeting gaps. Additionally, the Zomato MCP provides real-time restaurant availability, menu items, and cart pricing as external data inputs.

## Known Limitations
One major constraint is that the agent cannot dynamically top up the Zomato wallet if the balance falls below the cart total. Another known issue is that the agent may fail to complete the checkout if the Zomato API rate limits the connection or returns malformed restaurant data.