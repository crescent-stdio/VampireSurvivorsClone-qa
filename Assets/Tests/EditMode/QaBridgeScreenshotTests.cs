using System;
using System.Collections.Generic;
using System.IO;
using NUnit.Framework;
using Vampire.QA;

namespace Vampire.Tests.EditMode
{
    public sealed class QaBridgeScreenshotTests
    {
        private string temporaryDirectory;

        [SetUp]
        public void SetUp()
        {
            temporaryDirectory = Path.Combine(
                Path.GetTempPath(),
                "qa-bridge-screenshot-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(Path.Combine(temporaryDirectory, "bridge"));
        }

        [TearDown]
        public void TearDown()
        {
            if (Directory.Exists(temporaryDirectory))
                Directory.Delete(temporaryDirectory, true);
        }

        [Test]
        public void Capture_pending_frame_uses_fixed_session_path_and_completes_synchronously()
        {
            var events = new List<string>();
            var capture = new RecordingCapture(events);
            string bridgeDirectory = Path.Combine(temporaryDirectory, "bridge");

            string path = QaBridgeScreenshotService.CapturePendingFrame(
                bridgeDirectory,
                capture);
            events.Add("returned");

            Assert.That(path, Is.EqualTo(Path.Combine(
                temporaryDirectory,
                "final-frame.pending.png")));
            Assert.That(events, Is.EqualTo(new[] { "capture", "returned" }));
            Assert.That(File.Exists(path), Is.True);
        }

        private sealed class RecordingCapture : IQaFailureScreenshotCapture
        {
            private readonly List<string> events;

            public RecordingCapture(List<string> events)
            {
                this.events = events;
            }

            public void Capture(string path)
            {
                events.Add("capture");
                File.WriteAllBytes(path, new byte[] { 1, 2, 3 });
            }
        }
    }
}
