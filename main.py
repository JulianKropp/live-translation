import io
import tempfile
import time
from typing import List, Optional
import whisper # type: ignore
import torch
from pydub import AudioSegment # type: ignore

from extract_ogg import get_header_frames, split_ogg_data_into_frames, OggSFrame

from stream_pipeline.grpc_server import GrpcServer
from stream_pipeline.data_package import DataPackage, DataPackageModule
from stream_pipeline.module_classes import ExecutionModule, ModuleOptions
from stream_pipeline.pipeline import Pipeline, ControllerMode, PipelinePhase, PipelineController


def calculate_frame_duration(current_granule_position, previous_granule_position, sample_rate=48000):
    if previous_granule_position is None:
        return 0.0  # Default value for the first frame
    samples = current_granule_position - previous_granule_position
    duration = samples / sample_rate
    return duration

class CreateNsAudioPackage(ExecutionModule):
    def __init__(self) -> None:
        super().__init__(ModuleOptions(
                                use_mutex=False,
                                timeout=5,
                            ),
                            name="Create10sAudioPackage"
                        )
        self.audio_data_buffer: List[OggSFrame] = []
        self.sample_rate: int = 48000
        self.last_n_seconds: int = 10
        self.current_audio_buffer_seconds: float = 0

    

        self.header_buffer: bytes = b''
        self.header_frames: Optional[List[OggSFrame]] = None

    def execute(self, dp: DataPackage[bytes], dpm: DataPackageModule) -> None:
        if dp.data:
            frame = OggSFrame(dp.data)

            if not self.header_frames:
                self.header_buffer += frame.raw_data
                id_header_frame, comment_header_frames = get_header_frames(self.header_buffer)
                # print(f"ID Header Frame: {id_header_frame}")
                # print(f"Comment Header Frames: {comment_header_frames}")

                if id_header_frame and comment_header_frames:
                    self.header_frames = []
                    self.header_frames.append(id_header_frame)
                    self.header_frames.extend(comment_header_frames)
                else:
                    dpm.success = False
                    dpm.message = "Could not find the header frames"
                    return

            

            last_frame: Optional[OggSFrame] = self.audio_data_buffer[-1] if len(self.audio_data_buffer) > 0 else None

            current_granule_position: int = frame.header['granule_position']
            previous_granule_position: int = last_frame.header['granule_position'] if last_frame else 0

            frame_duration: float = calculate_frame_duration(current_granule_position, previous_granule_position, self.sample_rate)
            previous_granule_position = current_granule_position


            self.audio_data_buffer.append(frame)
            self.current_audio_buffer_seconds += frame_duration

            # Every second, process the last n seconds of audio
            if frame_duration > 0.0:
                if self.current_audio_buffer_seconds >= self.last_n_seconds:
                    # pop audio last frame from buffer
                    pop_frame = self.audio_data_buffer.pop(0)
                    pop_frame_granule_position = pop_frame.header['granule_position']
                    next_frame_granule_position = self.audio_data_buffer[0].header['granule_position'] if len(self.audio_data_buffer) > 0 else pop_frame_granule_position
                    pop_frame_duration = calculate_frame_duration(next_frame_granule_position, pop_frame_granule_position, self.sample_rate)
                    self.current_audio_buffer_seconds -= pop_frame_duration

                # print(f"Current audio buffer seconds: {self.current_audio_buffer_seconds}")

                # Combine the audio buffer into a single audio package
                n_seconds_of_audio: bytes = self.header_buffer + b''.join([frame.raw_data for frame in self.audio_data_buffer])
                dp.data = n_seconds_of_audio



class Whisper(ExecutionModule):
    def __init__(self) -> None:
        super().__init__(ModuleOptions(
                                use_mutex=True,
                                timeout=5,
                            ),
                            name="Whisper"
                        )
        self.ram_disk_path = "/mnt/ramdisk" # string: Path to the ramdisk
        self.task = "translate"             # string: transcribe, translate (transcribe or translate it to english)
        self.model = "tiny"                 # string: tiny, base, small, medium, large (Whisper model to use)
        self.models_path = ".models"        # string: Path to the model
        self.english_only = False           # boolean: Only translate to english

        if self.model != "large" and self.english_only:
            self.model = self.model + ".en"

        print(f"Loading model '{self.model}'...")
        self._whisper_model = whisper.load_model(self.model, download_root=self.models_path)
        print("Model loaded")
        
    
    def execute(self, dp: DataPackage[bytes], data_package_module: DataPackageModule) -> None:
        if dp.data:
            print(f"Processing {len(dp.data)} bytes of audio data")
            if dp.data:
                with tempfile.NamedTemporaryFile(prefix='tmp_audio_', suffix='.wav', dir=self.ram_disk_path, delete=True) as temp_file:
                    # Convert opus to wav
                    opus_data = io.BytesIO(dp.data)
                    opus_audio = AudioSegment.from_file(opus_data, format="ogg", frame_rate=48000, channels=2, sample_width=2)
                    opus_audio.export(temp_file.name, format="wav")
                    
                    # Transcribe audio data
                    result = self._whisper_model.transcribe(temp_file.name, fp16=torch.cuda.is_available(), task=self.task)
                    text = result['text'].strip()
                    print(f"Transcribed text: {text}")



controllers = [
    PipelineController(
        mode=ControllerMode.NOT_PARALLEL,
        max_workers=1,
        name="CreateNsAudioPackage",
        phases=[
            PipelinePhase(
                modules=[
                    CreateNsAudioPackage()
                ]
            )
        ]
    ),

    PipelineController(
        mode=ControllerMode.ORDER_BY_SEQUENCE,
        max_workers=10,
        name="MainProcessingController",
        phases=[
            PipelinePhase(
                modules=[
                    Whisper()
                ]
            )
        ]
    )
]

pipeline = Pipeline[bytes](controllers, name="WhisperPipeline")

def callback(processed_data: DataPackage[bytes]) -> None:
    print(f"f")
    
def exit_callback(dp: DataPackage[bytes]) -> None:
    print(f"Exit: dropped")

def error_callback(dp: DataPackage[bytes]) -> None:
    print(f"Error: {dp.errors[0]}")

instance = pipeline.register_instance()

def simulate_live_audio_stream(file_path: str, sample_rate: int = 48000) -> None:
    with open(file_path, 'rb') as file:
        ogg_bytes: bytes = file.read()

    frames: List[OggSFrame] = split_ogg_data_into_frames(ogg_bytes)  # Assuming Frame is the type for frames
    previous_granule_position: Optional[int] = None

    for frame_index, frame in enumerate(frames):
        current_granule_position: int = frame.header['granule_position']
        frame_duration: float = calculate_frame_duration(current_granule_position, previous_granule_position, sample_rate)
        previous_granule_position = current_granule_position

        # Sleep to simulate real-time audio playback
        time.sleep(frame_duration)

        pipeline.execute(frame.raw_data, instance, callback, exit_callback, error_callback)

if __name__ == "__main__":
    # Path to the Ogg file
    file_path: str = './audio/audio.ogg'
    start_time: float = time.time()
    simulate_live_audio_stream(file_path)
    end_time: float = time.time()

    execution_time: float = end_time - start_time
    print(f"Execution time: {execution_time} seconds")