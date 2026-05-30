# GUI TO-DO List
Just my operator notes on what does/doesn't work in the web GUI, and my thoughts on how it should work.

## v0.9.2 Implementation Status
The v0.9.2 cockpit-first overhaul is implemented. The header has agent, idle, blocked, and task
selectors plus the Cockpit/Ops Room toggle. Cockpit widgets now cover uptime, service health,
available models, token usage/spend, mission, alerts, tasks, teams, selected agent, orchestrator
chat, and settings/status. Ops Room keeps the visual room with agent Tasks, Chat, and Goals tabs,
and mission editing stays in Cockpit. Remaining refinements for room visualization, seat tools,
agent overlays, and deeper interaction polish move to the v0.9.4 iterative Ops Room pass.

## Header
Once we're past the point where we have the JWT Token input on the page, let's condense the header and first status bar to one line.
 - Agents
   - This should be a dropdown where you can select any agent, and have the same effect as clicking them in the room.
   - All agents should be listed, and have some sort of designation (TBD) on what their status is.
   - Selecting an agent will move the user to the Ops Room View to interact with the agent.
 - Working
   - We should change this to Idle, and it should also be a dropdown, but only have the filtered list of idle agents.
   - Selecting and agent from this list will act as if they were selected in the room, and switch to the Tasks tab if not already selected.
 - Blocked
   - This should also be a dropdown, filtered to only blocked agents.
   - In the dropdown should be a status about why they are blocked.  This may include (but is not limited to:
     - Needs User Input
     - Tool Call Failure
     - Awaiting Another Agent
   - Agents blocked awating the User should be at the top of the list, and cause it to be highlighted.
 - Tasks
   - This should be a list of all active and queued tasks.
   - Sort by Active then queued, then oldest first.
   - This will not include blocked tasks, as those will be under the blocked menu.

To the farthest right on this pane should be the toggle between the **Ops Room View** and the **Cockpit View**.

## Ops Room View
The **Ops Room View** is where the user can watch and interact with agents directly.
It is intended to be a visual representation of the live agents working.
The Orchestrator also has an avatar in ths room, but selecting the Orchestrator will move the user to the Cockpit View.

### Right Pane - Status Area
After selecting an agent this should show a status area.  The current one is ok for now.
Below the status area should be Three Tabs:
 - Chat
   - This should be a live chat window similar to every other chatbot interface, with the text input at the bottom.
   - This is for conversation between the agent and the operator only.  Heartbeats, cron jobs, orchestrator tasks should not appear in this chat.
 - Goals
   - These are the goals for the agent. They are maliable and can be changed.
   - The interface should have the label for each goal outside the box to prevent confusion.
   - Existing and completed goals should be below the Add Goal area (similar to how it is now.)
 - Tasks
   - This should primarialy be a list of tasks the agent is working on or has queued.
   - At the bottom should be a button to add a user task.
     - The user task button should pop up a dialog to capture the new task information.
     - Completing the new task dialog refreshes the task list.

*Note:* The Mission should not be on a tab at the agent level.  The mission is not intended to be malleable.  It should only be accessible in the "Cockpit View" where the user directly interacts with the Orchestrator.

### Left Pane - Room View
This is the Pixel Agents implementation.

## Cockpit View
This is intended to be the user's interface to the orchestrator and primary dashboard.  This will be like a 'Mission Control' board with the direct chat interface to The Orchestrator.
This view should be customiable for each user.  Use a widget based design similar to the TrueNAS web interface.
Initial widgets should include:
 - Uptime
 - Service Health
 - Models Available
 - Token Usage / Spend
 - Current Mission
 - Current Alerts (Orchestrator or Agent requests waiting user response)
 - Orchestrator Chat

The design of the dashboard and widgets should be such that a new widget can be designed and inserted easily.  Each user's interface should be able to show what information is most important to them.  It needs to be able to evolve with the user and the agents and their needs.  Agents should be able to build widgets either on request or to help solve a problem.

## Notes

The naming convention for Ops Room View and Cockpit View are open to discussion.
