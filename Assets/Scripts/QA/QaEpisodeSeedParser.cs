using System;

namespace Vampire
{
    public static class QaEpisodeSeedParser
    {
        public static bool TryParseQaSeed(string[] arguments, out int seed)
        {
            seed = 0;
            if (arguments == null)
                return false;

            for (var index = 0; index < arguments.Length; index++)
            {
                var argument = arguments[index];
                if (argument == null || !argument.StartsWith("-qaSeed=", StringComparison.Ordinal))
                    continue;

                return int.TryParse(argument.Substring("-qaSeed=".Length), out seed);
            }

            return false;
        }
    }
}
