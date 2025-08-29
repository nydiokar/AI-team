"""
Agent Manager - Loads and manages modular task agents
"""
import logging
from pathlib import Path
from typing import Dict, Optional, Type
from abc import ABC

from .interfaces import IAgent, TaskType
from config import config

logger = logging.getLogger(__name__)

class BaseAgent(ABC):
    """Base class for all agents with common functionality"""
    
    def __init__(self, agent_name: str, instructions: str, allowed_tools: list[str], 
                 should_modify_files: bool, validation_thresholds: dict[str, float]):
        self._agent_name = agent_name
        self._instructions = instructions
        self._allowed_tools = allowed_tools
        self._should_modify_files = should_modify_files
        self._validation_thresholds = validation_thresholds
    
    def get_agent_name(self) -> str:
        return self._agent_name
    
    def get_agent_instructions(self) -> str:
        return self._instructions
    
    def get_allowed_tools(self) -> list[str]:
        return self._allowed_tools
    
    def should_modify_files(self) -> bool:
        return self._should_modify_files
    
    def get_validation_thresholds(self) -> dict[str, float]:
        return self._validation_thresholds

class AgentManager:
    """Manages loading and configuration of modular task agents"""
    
    def __init__(self, agents_dir: str = "prompts/agents"):
        self.agents_dir = Path(agents_dir)
        self.agents: Dict[str, BaseAgent] = {}
        self._load_agents()
    
    def _load_agents(self):
        """Load all available agents from the agents directory"""
        # Check if agents are enabled via configuration
        if not config.system.agents_enabled:
            logger.info("Agents disabled via configuration (AGENTS_ENABLED=false)")
            return
            
        if not self.agents_dir.exists():
            logger.warning(f"Agents directory not found: {self.agents_dir}")
            return
        
        # Define agent configurations
        agent_configs = {
            "analyze": {
                "allowed_tools": ["Read", "LS", "Grep", "Glob"],
                "should_modify_files": False,
                "validation_thresholds": {"similarity": 0.6, "entropy": 0.7}
            },
            "bug_fix": {
                "allowed_tools": ["Read", "Edit", "MultiEdit", "LS", "Grep", "Glob", "Bash"],
                "should_modify_files": True,
                "validation_thresholds": {"similarity": 0.8, "entropy": 0.8}
            },
            "code_review": {
                "allowed_tools": ["Read", "LS", "Grep", "Glob"],
                "should_modify_files": False,
                "validation_thresholds": {"similarity": 0.7, "entropy": 0.8}
            },
            "documentation": {
                "allowed_tools": ["Read", "Edit", "MultiEdit", "LS", "Grep", "Glob"],
                "should_modify_files": True,
                "validation_thresholds": {"similarity": 0.6, "entropy": 0.7}
            }
        }
        
        for agent_name, agent_config in agent_configs.items():
            agent_file = self.agents_dir / f"{agent_name}.md"
            if agent_file.exists():
                try:
                    instructions = self._load_agent_instructions(agent_file)
                    agent = BaseAgent(
                        agent_name=agent_name,
                        instructions=instructions,
                        allowed_tools=agent_config["allowed_tools"],
                        should_modify_files=agent_config["should_modify_files"],
                        validation_thresholds=agent_config["validation_thresholds"]
                    )
                    self.agents[agent_name] = agent
                    logger.info(f"Loaded agent: {agent_name}")
                except Exception as e:
                    logger.error(f"Failed to load agent {agent_name}: {e}")
            else:
                logger.warning(f"Agent file not found: {agent_file}")
    
    def _load_agent_instructions(self, agent_file: Path) -> str:
        """Load agent instructions from markdown file"""
        with open(agent_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Extract the main instructions (everything after the first few lines)
        lines = content.split('\n')
        # Skip the first few lines (title, principles, examples) and get the core instructions
        if len(lines) > 10:
            return '\n'.join(lines[10:]).strip()
        return content.strip()
    
    def get_agent(self, agent_name: str) -> Optional[BaseAgent]:
        """Get an agent by name"""
        return self.agents.get(agent_name)
    
    def get_agent_for_task_type(self, task_type: TaskType) -> Optional[BaseAgent]:
        """Get the appropriate agent for a task type"""
        # Map task types to agent names
        type_to_agent = {
            TaskType.ANALYZE: "analyze",
            TaskType.FIX: "bug_fix",
            TaskType.BUG_FIX: "bug_fix",
            TaskType.CODE_REVIEW: "code_review",
            TaskType.DOCUMENTATION: "documentation",
            TaskType.SUMMARIZE: "analyze"  # Summarize uses analyze agent
        }
        
        agent_name = type_to_agent.get(task_type)
        if agent_name:
            return self.agents.get(agent_name)
        
        # Fallback to analyze agent
        return self.agents.get("analyze")
    
    def get_all_agents(self) -> Dict[str, BaseAgent]:
        """Get all loaded agents"""
        return self.agents.copy()
    
    def reload_agents(self):
        """Reload all agents from disk"""
        self.agents.clear()
        self._load_agents()
