using UnityEngine;
using UnityEngine.SceneManagement;
using Vampire.QA;

namespace Vampire
{
    public class LevelManager : MonoBehaviour
    {
        [SerializeField] private LevelBlueprint levelBlueprint;
        [SerializeField] private Character playerCharacter;
        [SerializeField] private EntityManager entityManager;
        [SerializeField] private AbilityManager abilityManager;
        [SerializeField] private AbilitySelectionDialog abilitySelectionDialog;
        [SerializeField] private InfiniteBackground infiniteBackground;
        [SerializeField] private Inventory inventory;
        [SerializeField] private StatsManager statsManager;
        [SerializeField] private GameOverDialog gameOverDialog;
        [SerializeField] private GameTimer gameTimer;
        private float levelTime = 0;
        private float timeSinceLastMonsterSpawned;
        private float timeSinceLastChestSpawned;
        private bool miniBossSpawned = false;
        private bool finalBossSpawned = false;
        private bool initialized = false;
        private QaEpisodeOutcome outcome = QaEpisodeOutcome.InProgress;

        public bool Initialized { get => initialized; }
        public float LevelTime { get => levelTime; }
        public bool MiniBossSpawned => miniBossSpawned;
        public bool FinalBossSpawned => finalBossSpawned;
        public QaEpisodeOutcome Outcome { get => outcome; }

        public void Init(LevelBlueprint levelBlueprint)
        {
            this.levelBlueprint = levelBlueprint;
            levelTime = 0;
            outcome = QaEpisodeOutcome.InProgress;
            initialized = false;
            
            // Initialize the entity manager
            entityManager.Init(this.levelBlueprint, playerCharacter, inventory, statsManager, infiniteBackground, abilitySelectionDialog);
            // Initialize the ability manager
            abilityManager.Init(this.levelBlueprint, entityManager, playerCharacter, abilityManager);
            abilitySelectionDialog.Init(abilityManager, entityManager, playerCharacter);
            // Initialize the character
            playerCharacter.Init(entityManager, abilityManager, statsManager);
            playerCharacter.OnDeath.AddListener(GameOver);
            // Spawn initial gems
            entityManager.SpawnGemsAroundPlayer(this.levelBlueprint.initialExpGemCount, this.levelBlueprint.initialExpGemType);
            // Spawn a singular chest
            entityManager.SpawnChest(levelBlueprint.chestBlueprint);
            // Initialize the infinite background
            infiniteBackground.Init(this.levelBlueprint.backgroundTexture, playerCharacter.transform);
            // Initialize inventory
            inventory.Init();
            initialized = true;
        }

        // Start is called before the first frame update
        void Start()
        {
            Init(levelBlueprint);
        }

        // Update is called once per frame
        void Update()
        {
            // Time
            levelTime += Time.deltaTime;
            gameTimer.SetTime(levelTime);
            // Monster spawning timer
            bool regularSpawnScheduleActive = levelTime < levelBlueprint.levelTime;
            float spawnRate = regularSpawnScheduleActive
                ? levelBlueprint.monsterSpawnTable.GetSpawnRate(levelTime/levelBlueprint.levelTime)
                : 0f;
            float monsterSpawnDelay = spawnRate > 0 ? 1.0f/spawnRate : float.PositiveInfinity;
            QaFaultTelemetry.ObserveRegularMonsterSpawnSchedule(
                levelTime,
                regularSpawnScheduleActive && spawnRate > 0,
                monsterSpawnDelay);
            if (regularSpawnScheduleActive && QaFaultInjection.ShouldSpawnRegularMonster(levelTime))
            {
                timeSinceLastMonsterSpawned += Time.deltaTime;
                if (timeSinceLastMonsterSpawned >= monsterSpawnDelay)
                {
                    (int monsterIndex, float hpMultiplier) = levelBlueprint.monsterSpawnTable.SelectMonsterWithHPMultiplier(levelTime/levelBlueprint.levelTime);
                    (int poolIndex, int blueprintIndex) = levelBlueprint.MonsterIndexMap[monsterIndex];
                    MonsterBlueprint monsterBlueprint = levelBlueprint.monsters[poolIndex].monsterBlueprints[blueprintIndex];
                    entityManager.SpawnMonsterRandomPosition(poolIndex, monsterBlueprint, monsterBlueprint.hp * hpMultiplier);
                    QaFaultTelemetry.RecordRegularMonsterSpawn(levelTime);
                    timeSinceLastMonsterSpawned = Mathf.Repeat(timeSinceLastMonsterSpawned, monsterSpawnDelay);
                }
            }
            // Boss spawning
            if (!miniBossSpawned && levelTime > levelBlueprint.miniBosses[0].spawnTime)
            {
                miniBossSpawned = true;
                entityManager.SpawnMonsterRandomPosition(levelBlueprint.monsters.Length, levelBlueprint.miniBosses[0].bossBlueprint);
            }
            // Boss spawning
            if (!finalBossSpawned && levelTime > levelBlueprint.levelTime)
            {
                //entityManager.KillAllMonsters();
                finalBossSpawned = true;
                Monster finalBoss = entityManager.SpawnMonsterRandomPosition(levelBlueprint.monsters.Length, levelBlueprint.finalBoss.bossBlueprint);
                finalBoss.OnKilled.AddListener(LevelPassed);
            }
            // Chest spawning timer
            timeSinceLastChestSpawned += Time.deltaTime;
            if (timeSinceLastChestSpawned >= levelBlueprint.chestSpawnDelay)
            {
                for (int i = 0; i < levelBlueprint.chestSpawnAmount; i++)
                {
                    entityManager.SpawnChest(levelBlueprint.chestBlueprint);
                }
                timeSinceLastChestSpawned = Mathf.Repeat(timeSinceLastChestSpawned, levelBlueprint.chestSpawnDelay);
            }
        }

        public void GameOver()
        {
            if (!initialized) return;
            if (!TryTransitionToOutcome(QaEpisodeOutcome.PlayerDied)) return;

            Time.timeScale = 0;
            int coinCount = PlayerPrefs.GetInt("Coins");
            PlayerPrefs.SetInt("Coins", coinCount + statsManager.CoinsGained);
            gameOverDialog.Open(false, statsManager);
        }

        public void LevelPassed(Monster finalBossKilled)
        {
            if (!initialized) return;
            if (!TryTransitionToOutcome(QaEpisodeOutcome.Passed)) return;

            Time.timeScale = 0;
            int coinCount = PlayerPrefs.GetInt("Coins");
            PlayerPrefs.SetInt("Coins", coinCount + statsManager.CoinsGained);
            gameOverDialog.Open(true, statsManager);
        }

        public bool TryTransitionToOutcome(QaEpisodeOutcome terminalOutcome)
        {
            if (terminalOutcome != QaEpisodeOutcome.Passed &&
                terminalOutcome != QaEpisodeOutcome.PlayerDied &&
                terminalOutcome != QaEpisodeOutcome.TimedOut &&
                terminalOutcome != QaEpisodeOutcome.Error)
                return false;

            if (outcome != QaEpisodeOutcome.InProgress)
                return false;

            outcome = terminalOutcome;
            return true;
        }

        public void Restart()
        {
            Time.timeScale = 1;
            SceneManager.LoadScene(SceneManager.GetActiveScene().name);
        }

        public void ReturnToMainMenu()
        {
            Time.timeScale = 1;
            SceneManager.LoadScene(0);
        }
    }
}
