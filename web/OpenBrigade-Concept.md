# The Brigade Harness
A new style of orchestrated agent harness, built around Escoffe's Kitchen Brigade System.

## Project Description
After working with Openclaw for about 3 months, I found myself writing plugins to compensate for a lot of the issues that I kept running into.  The biggest were long-term memory, continuity, and building a useful knowledge base.  This project aims to build a new harness layer with the lessons learned from all of these, as well and looking to existing systems for guidance.

### End goal
In the end, the aim of the first full release is to create a harness for autonomous agents that can both be continously working towards a combined mission, and working with their human operator as necessary.

### The Brigade Premise
Based on the concept of Escoffe's Kitchen Brigade, the human would be the Owner and Executive Chef.  The Crew Chiefs would be Agents who act as the Sous Chefs in charge of their teams/areas, and Agents would be the line workers; specialized, but doing the bulk of the work.
*Using this scenario, the Orchestrator would be the nosy Matre De who makes sure everything is running, but does none of the work themselves.*
---
# The Harness
The harness needs to be brigade based and hierarchial.
It also needs to force clean boundaries, while allowing for cross boundary querying.  (Yes it sounds contradictory, but I have a plan!)

## Interface
Like OpenClaw, there are several ways to work with the agents.

### Chat Sessions
Human Chat Sessions are the primary interface with the agents.  These may be through chat bots, a web interface (GUI), a text interface (TUI), and should be able to be expanded on in the future.
The human user(s) can talk to any Agent at any time.  That agent should be able to:
- Explain what it is working on, what it has been working on, and the status of any of it's tasks.
- What it's goal is, and how it's task is related to the goal.
- It's main function in the brigade, and any additional jobs it preforms.
- Any blockers stopping it from completing tasks.

#### Chat Surfaces
Initial MVP will include:
- Full Screen TUI
- Telegram Bots
- Basic web Interface

This will be expanded as necessary and resource availability dictate.
Planned extensions:
- Discord
- Slack
- Google Chat
- Signal

---
## Sessions
The harness needs to silo sessions by type.  The primary session type will be the Heartbeat.  The human facing sessions will be chats.  The third type are cron jobs, which should be fire-and-forget.
The sessions will all write to the same agent history, both fulltext and summarized.  This history is available across all session types for that agent.

### Heartbeats
These are the Orchestrator managed proactive tasks.  The bulk of the work will be done in these sessions.  The work done in these sessions will be archived, but not transmittedto the user in real time.
Effectively a recurring cron job.  When the Orchestrator service fires, it will check the status of all the outstanding jobs.  It will give direction to each agent in their Heartbeat file/record.  The Agent will preform the task on it's next heartbeat.

Heartbeat sessions will build the first prompt from the system prompt, the task, and only highly relevant vector pulls.  From there the agent is expected to work to the goal of completing the task assigned.  
The agent may use as many turns as necessary to complete the task.  When the task is completed, the agent will update the status of the job in the queue.  The job is either completed, failed, ongoing, or blocked.  The status record will also have the summary of the session that was added to QDrant, and if necessary and blocks listed out specifically.
If a task is 'ongoing' that means the agent has not completed but has had to stop the processing for some reason.  The orchestrator will reassign this task for completion in the next cycle.

### Cron jobs
Simple or repetitive tasks that the agent does on a regular schedule.
- Morning Briefing
- E-Mail checking
- Daily eBay searches
- Stock price lookups
- Maintenance scripts

These jobs fire and forget, logging the result to the full text history only.

### User Chat
This is the bulk of where the user will spend their time.  These chats should be siloed from the others initially.
The starting prompt should be built from the system prompt, the agent's personality files, and a rumination cycle. (See below)
These are the place for the user to talk to the agent.
- Ask about projects, tasks, goals, etc.
- Do research with the Agent's assistance.
- Preform tasks with or without regard to the overall mission or goal.
- Let the agent be inquisitive and ask questions of the user.
- Just chat with the agent.
Basically, anything the user wants to do with the agent.
This is also the main control surface for defining the mission of the team, goal of the crew or agent, add tasks, give responses to questions, or anything else that requires user intervention.

If the user asks about a project, goal, task, etc., the agent will search the histories for that specific information to update the user.  If the user wants to add context or update the task or goal, the agent should update the file/record with that data.  If the user asks to kill a task, the orchestrator will need to determine if just stopping it is ok, or if there needs to be cleanup first.

### Inter-Agent Chats
From my experience, when you assign agents specific roles, they will invariably want to do one of two things:  Ignore them and try to do everything themselves, or handle the handoff to the other agent.  In either case, you need a way for the agents to communicate.
The [session-send] command in OpenClaw is functional, but difficult, and very messy.  It does deliver a message from one agent to another, and waits for a response.  However, if the agent is not currently active, then it ends up timing out and the whole system becomes passive.
We also tried creating a Telegram group chat, but found that the bots would only respond to the user, and usually all simultaneously.  At one point we were able to use the Telegram Group Chat as the conversation marker for background chats, but ran into the same issue, if an agent wasn't active then it would wait, and sometimes never get responded to.

#### Active Inter-Agent Chats
This is where it's going to get tricky.  One agent to another should be simple, create a new chat session, have the conversation, archive the conversation to each agent's histories, put the summary in the vector store with both agents tagged.  Return the summary and the full conversation ID to the requesting chat for the initiating agent to reference.  Then the session is closed and cleared.

Inter-Agent group chats still need some research.  The best I've seen my team do so far is to 'Round Robbin' the chat, where they each take turns, but basically it became a mess of writing to the other's chat session and it not being entirely sure who sent the message unless it was tagged.  My goal is that like a single agent-to-agent chat session, they would all participate in one space, all have the full conversation written to their individual histories, and the summary in the vector store.  The conversation session is cleared, and the initiator gets the summary and session ID back as a result.
The logic determining the chat order still needs a lot of work.  Meta-reasoning will be a big part of this, I think.

---
## The Agents
```
An AI agent is a software program that acts autonomously to achieve specific goals, utilizing Large Language Models (LLMs) to reason, plan, and use external tools with minimal human oversight. Unlike standard chatbots, which only generate text, AI agents take actions, such as browsing the web, accessing databases, or executing workflows across multiple apps. (Source: IBM)
```

The agents we are building are autonomous actors.  However, they are also the primary control surface for the users.  They will have long term memory, personalities, are intended to be inquisitive, have opinions, and most importantly be engaging.  *Nobody wants a tool that's no fun to use.*

### What the Harness does
The harness is intended to be the means that the agent accesses the models used to complete tasks.
Typically these will be LLMs, whether local or cloud hosted.
We will need initially to be able to work with:
- Ollama (Local Inference, embedding)
- OpenAI/Codex (API calls and Codex oAuth)
- Anthropic/Claude (API Calls)
- Google/Gemini (API Calls or oAuth)

### Who the Agents Are
The Agents have several core personal files.
AGENTS.md - Core rules about being an agent and using your workspace.
USER.md - Their knowledge and understanding of the user(s).
IDENTITY.md - Who Am I?  A brief description of your name, role, personality, and selected emoji avatar.
MEMORY.md - Core memories, curated.
TOOLS.md - Local notes on using skills, specific to your needs.
SOUL.md - Who You Are - The core truths you believe, boudaries, standing permissions, and general vibe.

### Agentic MEMORY
All agents can access the shared library, knowledge graph, and vector database.  So they will have some knowledge of what other agents are doing.  They only have access to their own full context memory.

The most impotant and curated memories should be stored in MEMORY.md for fastest retrieval.
Daily/Session notes should be stored in memory/YYYYMMDD-MEMORY.md, only append when writing.
Memory curation is a daily cron job that looks at the information in memory/YYYYMMDD-MEMORY.md and decideds what to elevate to MEMORY.md, and if anything should be removed from MEMORY.md.  MEMORY.md should never exceed 2KB.

### HEARTBEATs
On a regular basis cron will launch HEARTBEAT cycles for each agent.  The main purpose is to complete the task laid out in HEARTBEAT.md by the Orchestrator and report back.
The top part of the file will have general information for running heartbeats, but the end of the file will be a structured JSON block.  This will give the specific task to complete.  This will include the Redis job ID.
- If the task is completed, the agent shall update the Redis record with status 'completed', and a very short description of the run.  The status field in Redis is only meant to be the Executive Summary of the run.  The Orchestrator does not need the details.
- If the task fails, the agent shall update the Redis record with status 'failed', and the specific failure information.
- If a task is blocked, the agen shall update the Redis record with stats 'blocked', and what the block is that needs to be cleared.  If the block can only be cleared by the user, they may message that information to the user before closing the session.
- If a task cycle ends, but the task is incomplete, the agent shall update the Redis record with 'in progress - turn ##' where ## is the current count of turns taken on the task.  If it gets to 10, assume the task failed and mark as 'abandoned'.  If possible, update the status with what the sticking point is.

---
## The Orchestrator
The orchestrator is a service, not an agent.  It controls the mission.
**The Mission** - The overarching goal of the whole brigade.  Tasks are assigned and goals are set in order to complete the mission.  The Orchestrator is the only one who sees the mission in every cycle.
**Goals** - Per Agent alignment goals.  The Agent will use this as a guide in making decisions, the Orchestrator will use it to determine if the Agent stays in alignment.
**Tasks** - The specific task queued for a heartbeat session.  This can be assigned by the Orchestrator to anyone, the Crew Chief to it's Agents, or the User anyone.
- User Assigned tasks take priority.
- A Task must produce a meaningful outcome.  Failure is acceptable, it gives data and history to work from.  Doing no work, or doing work that does not advance the goal or mission is unacceptable.

The Orchestrator works via it's own reasoning.  This is logged in it's own table, not the conversations history table.  It evaluates what was assigned in the last cycle and the state of the task.  It should aim to resolve blocks to current tasks before moving an agent on the same path.  It should assign any failure analysis to an agent to report back before reassigning the task.

### Orchestrator Run Sequence

```
1. Load mission
2. Pull agent state snapshots / task updates
3. Resolve blocks/failures
4. Check Task queue → if user inputs exist, evaluate before anything else
5. Load own previous reasoning
6. Make assignment decisions
7. Write assignments to agent states
8. IF long-form work queued → assign through agent using elevated model options
9. Log own reasoning for next run
10. Sleep
```

### State tracking

Redis is the canonical runtime state store for active orchestrator state, active assignment records, pending queues, and local inference lock state (if used).

Redis records do not expire automatically. Completed, failed, abandoned, or superseded assignment records are explicitly archived into the orchestrator’s PostgreSQL tables before being removed or compacted from Redis.

Completed assignment history is stored durably in the orchestrator’s PostgreSQL tables. Redis holds active runtime state; PostgreSQL holds completed history, audit records, assignment outcomes, and dispatch transcripts indexed by `assignment_id`.

---
## Dashboard Display
There needs to be a dashboard.
This dashboard should show the hierarchical listing of agents, what their current task is, and allow for digging into the agents for more information.

**Via the Dashboard the Mission and Agent Goals should be visible and editable.**

---
## Knowledge Library
The system must include document and repository libraries.
- Web articles rewritten in markdown and stored in full
- PDFs downloaded and stored 
- GitHub repos downloaded and zipped
- Texts and Books stored in Plaintext or markdown

The document ingestion system should identify the document type and source.
- Chunk and store in QDrant with approprite metadata
	- Single web pages and e-mails can be a single chunk if short.
	- Longer articles / PDFs should be divided into small and overlapping chunks for referencing.
	- Books and other significantly long texts should be broken down by chapter when possible, or into chunks larger than artricles.  Full chapter chunks do not need overlapping.
- Use metadata to create knowledge graph nodes and links
	- Author
	- Title
	- Type (Book, online article, paper, e-mail, blog post, etc.)
	- Category (Fiction, Non-Fiction, Academic Publication, Historical Text, ArXive)
	- Subject (Keywords)
	- Publication Date (Year)

---
## References:
- OpenClaw - https://github.com/openclaw/openclaw/releases/tag/v2026.5.7
- CrewAI - https://github.com/crewaiinc/crewai
- Memory-house documentation and code (local)
	- Incorporate learnings from our memory-house project.
	- Full context chat storage in SQL (Postgres)
	- Summarized chat logs in vectors (QDrant)
	- A working queue system in Redis
	- A knowledge graph of tasks and decisions (Neo4j).
- Mempalace - https://github.com/mempalace/mempalace
- OpenBrain - https://github.com/NateBJones-Projects/OB1
- PixelAgents - https://github.com/pablodelucca/pixel-agents
- Existing plugins
	- BetterAgent
	- Self Improving agent
	- Dream Cycles

---
#### Open Source Permissions
Distributed under the **GNU GPLv3 license**.  You may freely use, edit, and distribute the project, but all software derived from this project must remain open sourced and reference the original source.