using System;

namespace Vampire.QA
{
    [Serializable]
    public class QACommand
    {
        public string id;
        public string action;
        public float x;
        public float y;
        public float duration;
        public int index;
        public int seed;
        public bool collect_chests;
        public float chest_radius;
        public float threat_radius;
        public float survival_weight;
        public float min_forward_component;
        public float interrupt_health_ratio;
        public float interrupt_danger_score;
        public float interrupt_chest_distance;
        public string intent;
        public int target_id;
        public bool continue_during_planning;
    }

    [Serializable]
    public class VectorState
    {
        public float x;
        public float y;

        public VectorState() { }

        public VectorState(float x, float y)
        {
            this.x = x;
            this.y = y;
        }
    }

    [Serializable]
    public class PlayerState
    {
        public bool present;
        public bool alive;
        public VectorState position;
        public VectorState velocity;
        public float health;
        public float max_health;
        public float health_ratio;
        public int level;
        public float exp;
        public float next_level_exp;
        public float exp_ratio;
    }

    [Serializable]
    public class EntityPositionState
    {
        public int id;
        public string kind;
        public float x;
        public float y;
        public float relative_x;
        public float relative_y;
        public float distance;
    }

    [Serializable]
    public class WorldState
    {
        public int enemy_count;
        public int pickup_count;
        public int chest_count;
        public float nearest_enemy_distance;
        public VectorState nearest_enemy_vector;
        public float nearest_chest_distance;
        public VectorState nearest_chest_vector;
        public float nearest_pickup_distance;
        public VectorState nearest_pickup_vector;
        public string nearest_pickup_kind;
        public int nearby_enemy_count;
        public float danger_score;
        public VectorState escape_vector;
        public int[] enemy_octants;
        public bool mini_boss_spawned;
        public bool final_boss_spawned;
        public EntityPositionState[] qa_entities;
        public EntityPositionState[] visible_chests;
    }

    [Serializable]
    public class ControllerState
    {
        public bool active;
        public string skill;
        public VectorState heading;
        public VectorState steering;
        public string target_kind;
        public float target_distance;
        public bool collect_chests;
        public float danger_score;
        public string planner_intent;
        public int target_id;
    }

    [Serializable]
    public class EventState
    {
        public string type;
        public string detail;
        public float level_time;
    }

    [Serializable]
    public class ProgressState
    {
        public float level_time;
        public int monsters_killed;
        public float damage_dealt;
        public float damage_taken;
        public int coins_gained;
    }

    [Serializable]
    public class AbilityChoiceState
    {
        public int index;
        public string name;
        public string description;
        public int level;
        public bool owned;
    }

    [Serializable]
    public class MenuState
    {
        public bool upgrade_open;
        public bool game_over;
        public int character_count;
        public AbilityChoiceState[] choices;
    }

    [Serializable]
    public class InventorySlotState
    {
        public int index;
        public string type;
        public int count;
        public int pending_count;
    }

    [Serializable]
    public class InventoryState
    {
        public InventorySlotState[] slots;
    }

    [Serializable]
    public class QAObservation
    {
        public string protocol_version = "1.3";
        public string command_id;
        public bool ok;
        public string result;
        public string timestamp_utc;
        public string mode;
        public int seed;
        public string scene;
        public string phase;
        public bool paused;
        public string pause_reason;
        public bool awaiting_agent_command;
        public float time_scale;
        public int frame;
        public PlayerState player;
        public WorldState world;
        public ProgressState progress;
        public MenuState menu;
        public InventoryState inventory;
        public ControllerState controller;
        public EventState event_state;
        public string[] available_actions;
        public string[] recent_logs;
    }

    [Serializable]
    public class QAReadyState
    {
        public string protocol_version = "1.3";
        public bool ready;
        public int process_id;
        public string bridge_directory;
        public string mode;
        public int seed;
    }
}

