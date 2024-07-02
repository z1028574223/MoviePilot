import shutil
from pathlib import Path
from typing import Set, Tuple, Optional, Union, List, Dict

from torrentool.torrent import Torrent
from transmission_rpc import File

from app import schemas
from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.modules import _ModuleBase
from app.modules.transmission.transmission import Transmission
from app.schemas import TransferTorrent, DownloadingTorrent
from app.schemas.types import TorrentStatus
from app.utils.string import StringUtils
from app.utils.system import SystemUtils


class TransmissionModule(_ModuleBase):
    _servers: Dict[str, Transmission] = {}
    _default_server: Transmission = None
    _default_server_name: str = None

    def init_module(self) -> None:
        # 读取下载器配置
        self._servers = {}
        downloaders = DownloaderHelper().get_downloaders()
        if not downloaders:
            return
        for server in downloaders:
            if server.type == "transmission":
                self._servers[server.name] = Transmission(**server.config)
                if server.default:
                    self._default_server_name = server.name
                    self._default_server = self._servers[server.name]

    @staticmethod
    def get_name() -> str:
        return "Transmission"
    
    def get_server(self, name: str = None) -> Optional[Transmission]:
        """
        获取服务器，name为空则返回默认服务器
        """
        if name:
            return self._servers.get(name)
        return self._default_server

    def stop(self):
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        if not self._servers:
            return False, "未配置Transmission下载器"
        for name, server in self._servers.items():
            if server.is_inactive():
                server.reconnect()
            if not server.transfer_info():
                return False, f"无法连接Transmission下载器：{name}"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次
        """
        # 定时重连
        for name, server in self._servers.items():
            if server.is_inactive():
                logger.info(f"Transmission下载器 {name} 连接断开，尝试重连 ...")
                server.reconnect()

    def download(self, content: Union[Path, str], download_dir: Path, cookie: str,
                 episodes: Set[int] = None, category: str = None,
                 downloader: str = None) -> Optional[Tuple[Optional[str], Optional[str], str]]:
        """
        根据种子文件，选择并添加下载任务
        :param content:  种子文件地址或者磁力链接
        :param download_dir:  下载目录
        :param cookie:  cookie
        :param episodes:  需要下载的集数
        :param category:  分类，TR中未使用
        :param downloader:  下载器
        :return: 下载器名称、种子Hash、错误原因
        """

        def __get_torrent_info() -> Tuple[str, int]:
            """
            获取种子名称
            """
            try:
                if isinstance(content, Path):
                    torrentinfo = Torrent.from_file(content)
                else:
                    torrentinfo = Torrent.from_string(content)
                return torrentinfo.name, torrentinfo.total_size
            except Exception as e:
                logger.error(f"获取种子名称失败：{e}")
                return "", 0

        if not content:
            return None, None, "下载内容为空"
        if isinstance(content, Path) and not content.exists():
            return None, None, f"种子文件不存在：{content}"

        # 获取下载器
        server = self.get_server(downloader)
        if not server:
            return None

        # 如果要选择文件则先暂停
        is_paused = True if episodes else False
        # 标签
        if settings.TORRENT_TAG:
            labels = [settings.TORRENT_TAG]
        else:
            labels = None
        # 添加任务
        torrent = server.add_torrent(
            content=content.read_bytes() if isinstance(content, Path) else content,
            download_dir=str(download_dir),
            is_paused=is_paused,
            labels=labels,
            cookie=cookie
        )
        if not torrent:
            # 读取种子的名称
            torrent_name, torrent_size = __get_torrent_info()
            if not torrent_name:
                return None, None, f"添加种子任务失败：无法读取种子文件"
            # 查询所有下载器的种子
            torrents, error = server.get_torrents()
            if error:
                return None, None, "无法连接transmission下载器"
            if torrents:
                for torrent in torrents:
                    # 名称与大小相等则认为是同一个种子
                    if torrent.name == torrent_name and torrent.total_size == torrent_size:
                        torrent_hash = torrent.hashString
                        logger.warn(f"下载器中已存在该种子任务：{torrent_hash} - {torrent.name}")
                        # 给种子打上标签
                        if settings.TORRENT_TAG:
                            logger.info(f"给种子 {torrent_hash} 打上标签：{settings.TORRENT_TAG}")
                            # 种子标签
                            labels = [str(tag).strip()
                                      for tag in torrent.labels] if hasattr(torrent, "labels") else []
                            if "已整理" in labels:
                                labels.remove("已整理")
                                server.set_torrent_tag(ids=torrent_hash, tags=labels)
                            if settings.TORRENT_TAG and settings.TORRENT_TAG not in labels:
                                labels.append(settings.TORRENT_TAG)
                                server.set_torrent_tag(ids=torrent_hash, tags=labels)
                        return downloader or self._default_server, torrent_hash, f"下载任务已存在"
            return None, None, f"添加种子任务失败：{content}"
        else:
            torrent_hash = torrent.hashString
            if is_paused:
                # 选择文件
                torrent_files = server.get_files(torrent_hash)
                if not torrent_files:
                    return downloader or self._default_server, torrent_hash, "获取种子文件失败，下载任务可能在暂停状态"
                # 需要的文件信息
                file_ids = []
                unwanted_file_ids = []
                for torrent_file in torrent_files:
                    file_id = torrent_file.id
                    file_name = torrent_file.name
                    meta_info = MetaInfo(file_name)
                    if not meta_info.episode_list:
                        unwanted_file_ids.append(file_id)
                        continue
                    selected = set(meta_info.episode_list).issubset(set(episodes))
                    if not selected:
                        unwanted_file_ids.append(file_id)
                        continue
                    file_ids.append(file_id)
                # 选择文件
                server.set_files(torrent_hash, file_ids)
                server.set_unwanted_files(torrent_hash, unwanted_file_ids)
                # 开始任务
                server.start_torrents(torrent_hash)
                return downloader or self._default_server, torrent_hash, "添加下载任务成功"
            else:
                return downloader or self._default_server, torrent_hash, "添加下载任务成功"

    def list_torrents(self, status: TorrentStatus = None,
                      hashs: Union[list, str] = None,
                      downloader: str = None
                      ) -> Optional[List[Union[TransferTorrent, DownloadingTorrent]]]:
        """
        获取下载器种子列表
        :param status:  种子状态
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: 下载器中符合状态的种子列表
        """
        # 获取下载器
        server = self.get_server(downloader)
        if not server:
            return None
        ret_torrents = []
        if hashs:
            # 按Hash获取
            torrents, _ = server.get_torrents(ids=hashs, tags=settings.TORRENT_TAG)
            for torrent in torrents or []:
                ret_torrents.append(TransferTorrent(
                    title=torrent.name,
                    path=Path(torrent.download_dir) / torrent.name,
                    hash=torrent.hashString,
                    size=torrent.total_size,
                    tags=",".join(torrent.labels or [])
                ))
        elif status == TorrentStatus.TRANSFER:
            # 获取已完成且未整理的
            torrents = server.get_completed_torrents(tags=settings.TORRENT_TAG)
            for torrent in torrents or []:
                # 含"已整理"tag的不处理
                if "已整理" in torrent.labels or []:
                    continue
                # 下载路径
                path = torrent.download_dir
                # 无法获取下载路径的不处理
                if not path:
                    logger.debug(f"未获取到 {torrent.name} 下载保存路径")
                    continue
                ret_torrents.append(TransferTorrent(
                    title=torrent.name,
                    path=Path(torrent.download_dir) / torrent.name,
                    hash=torrent.hashString,
                    tags=",".join(torrent.labels or [])
                ))
        elif status == TorrentStatus.DOWNLOADING:
            # 获取正在下载的任务
            torrents = server.get_downloading_torrents(tags=settings.TORRENT_TAG)
            for torrent in torrents or []:
                meta = MetaInfo(torrent.name)
                dlspeed = torrent.rate_download if hasattr(torrent, "rate_download") else torrent.rateDownload
                upspeed = torrent.rate_upload if hasattr(torrent, "rate_upload") else torrent.rateUpload
                ret_torrents.append(DownloadingTorrent(
                    hash=torrent.hashString,
                    title=torrent.name,
                    name=meta.name,
                    year=meta.year,
                    season_episode=meta.season_episode,
                    progress=torrent.progress,
                    size=torrent.total_size,
                    state="paused" if torrent.status == "stopped" else "downloading",
                    dlspeed=StringUtils.str_filesize(dlspeed),
                    upspeed=StringUtils.str_filesize(upspeed),
                    left_time=StringUtils.str_secends(torrent.left_until_done / dlspeed) if dlspeed > 0 else ''
                ))
        else:
            return None
        return ret_torrents

    def transfer_completed(self, hashs: str, path: Path = None,
                           downloader: str = None) -> None:
        """
        转移完成后的处理
        :param hashs:  种子Hash
        :param path:  源目录
        :param downloader:  下载器
        :return: None
        """
        # 获取下载器
        server = self.get_server(downloader)
        if not server:
            return None
        # 获取原标签
        org_tags = server.get_torrent_tags(ids=hashs)
        # 种子打上已整理标签
        if org_tags:
            tags = org_tags + ['已整理']
        else:
            tags = ['已整理']
        server.set_torrent_tag(ids=hashs, tags=tags)
        # 移动模式删除种子
        if settings.TRANSFER_TYPE in ["move", "rclone_move"]:
            if self.remove_torrents(hashs):
                logger.info(f"移动模式删除种子成功：{hashs} ")
            # 删除残留文件
            if path and path.exists():
                files = SystemUtils.list_files(path, settings.RMT_MEDIAEXT)
                if not files:
                    logger.warn(f"删除残留文件夹：{path}")
                    shutil.rmtree(path, ignore_errors=True)

    def remove_torrents(self, hashs: Union[str, list], delete_file: bool = True,
                        downloader: str = None) -> Optional[bool]:
        """
        删除下载器种子
        :param hashs:  种子Hash
        :param delete_file:  是否删除文件
        :param downloader:  下载器
        :return: bool
        """
        # 获取下载器
        server = self.get_server(downloader)
        if not server:
            return None
        return server.delete_torrents(delete_file=delete_file, ids=hashs)

    def start_torrents(self, hashs: Union[list, str],
                       downloader: str = None) -> Optional[bool]:
        """
        开始下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        # 获取下载器
        server = self.get_server(downloader)
        if not server:
            return None
        return server.start_torrents(ids=hashs)

    def stop_torrents(self, hashs: Union[list, str],
                      downloader: str = None) -> Optional[bool]:
        """
        停止下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        # 获取下载器
        server = self.get_server(downloader)
        if not server:
            return None
        return server.start_torrents(ids=hashs)

    def torrent_files(self, tid: str, downloader: str = None) -> Optional[List[File]]:
        """
        获取种子文件列表
        """
        # 获取下载器
        server = self.get_server(downloader)
        if not server:
            return None
        return server.get_files(tid=tid)

    def downloader_info(self, downloader: str = None) -> Optional[List[schemas.DownloaderInfo]]:
        """
        下载器信息
        """
        if downloader:
            server = self.get_server(downloader)
            if not server:
                return None
            servers = [server]
        else:
            servers = self._servers.values()
        # 调用Qbittorrent API查询实时信息
        ret_info = []
        for server in servers:
            info = server.transfer_info()
            if not info:
                continue
            ret_info.append(schemas.DownloaderInfo(
                download_speed=info.download_speed,
                upload_speed=info.upload_speed,
                download_size=info.current_stats.downloaded_bytes,
                upload_size=info.current_stats.uploaded_bytes
            ))
        return ret_info
