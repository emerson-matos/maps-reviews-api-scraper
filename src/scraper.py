from typing import List, Tuple, Union
import requests
import traceback
from datetime import datetime
import time
import math
import urllib.parse
from pathlib import Path
from lxml import etree, html
from bs4 import BeautifulSoup, Tag, NavigableString, Comment
import regex as re

from src.customlogger import get_logger
from src.config import sort_by_enum, review_default_result, metadata_default

default_hl = "pt-br"
default_request_interval = 0.2
default_n_retries = 3
default_retry_time = 3

Path("examples/").mkdir(exist_ok=True)


class GoogleMapsAPIScraper:
    def __init__(
        self,
        hl: str = default_hl,
        request_interval: float = default_request_interval,
        n_retries: int = default_n_retries,
        retry_time: float = default_retry_time,
        logger=None,
    ):
        if not logger is None:
            self.logger = logger
        else:
            self.logger = get_logger("google_maps_api_scraper")
        self.hl = hl
        self.request_interval = request_interval
        self.n_retries = n_retries
        self.retry_time = retry_time

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        if exc_type is not None:
            traceback.print_exception(exc_type, exc_value, tb)
            self.logger.exception(exc_value)

        self.driver.close()
        self.driver.quit()

        return True

    def _parse_url_to_feature_id(self, url: str) -> str:
        return re.findall("0[xX][0-9a-fA-F]+:0[xX][0-9a-fA-F]+", url)[0]

    def _parse_sort_by(self, sort_by: str) -> int:
        """Default to newest"""
        return sort_by_enum.get(sort_by, 1)

    def _get_api_response(
        self,
        feature_id: str,
        async_: str = "",
        hl: str = "",
        sort_by_id: int = "",
        associated_topic: str = "",
        token: str = "",
    ) -> Tuple[BeautifulSoup, List[BeautifulSoup], int, str]:
        if not hl:
            hl = self.hl
        query = (
            "https://www.google.com/async/reviewDialog?"
            f"hl={hl}&"
            f"async={async_}"
            f"feature_id:{feature_id},"
            f"sort_by:{sort_by_id},"
            f"next_page_token:{token},"
            f"associated_topic:{associated_topic},"
            f"_fmt:pc"
        )
        # Make request
        response = requests.get(query)
        # Decode response
        response = response.content.decode(encoding="unicode_escape")

        # Cut response to remove css
        response = self._cut_response(response)

        # Send page to soup and trees
        response_soup = BeautifulSoup(response, "lxml")
        tree = html.document_fromstring(response)
        # soup = BeautifulSoup(response, "html.parser")
        # dom = etree.HTML(response_soup.text)

        # Encontrando número de reviews e token de próxima página
        metadata_node = tree.xpath("//*[@data-google-review-count]")[0]
        review_count = int(metadata_node.attrib["data-google-review-count"])
        next_token = metadata_node.attrib["data-next-page-token"]

        # Iterando sobre texto de cada review
        reviews_tree = tree.xpath("/html/body/div[1]/div/div[2]/div[4]/div/div[2]/div")
        reviews_soup = [response_soup.find("div", dict(r.attrib)) for r in reviews_tree]

        return response_soup, reviews_soup, review_count, next_token

    def _cut_response(self, text: str) -> str:
        idx_first_div = text.find("<div ")
        m = re.search("</div>", text, flags=re.REVERSE)
        idx_last_div = m.span()[1]
        text = text[idx_first_div:idx_last_div]
        return "<html><body>" + text + "</body></html>"

    def _parse_place(
        self,
        response: BeautifulSoup,
    ) -> dict:
        metadata = metadata_default.copy()

        # Parse place_name
        try:
            metadata["place_name"] = response.find(True, class_="P5Bobd").text
        except Exception as e:
            self.logger.error("error parsing place: place_name")
            self.logger.exception(e)

        # Parse address
        try:
            metadata["address"] = response.find(True, class_="T6pBCe").text
        except Exception as e:
            self.logger.error("error parsing place: address")
            self.logger.exception(e)

        # Parse overall_rating
        try:
            rating_text = response.find(True, class_="Aq14fc").text.replace(",", ".")
            metadata["overall_rating"] = float(rating_text)
        except Exception as e:
            self.logger.error("error parsing place: overall_rating")
            self.logger.exception(e)

        # Parse n_reviews
        try:
            n_reviews_text = response.find(True, class_="z5jxId").text
            n_reviews_text = re.sub("[.]|,| reviews| comentários", "", n_reviews_text)
            metadata["n_reviews"] = int(n_reviews_text)
        except Exception as e:
            self.logger.error("error parsing place: n_reviews")
            self.logger.exception(e)

        # Parse topics
        try:
            topics = response.find("localreviews-place-topics")
            s = " ".join([s for s in topics.stripped_strings])
            metadata["topics"] = re.sub("\s+", " ", s)
        except Exception as e:
            self.logger.error("error parsing place: topics")
            self.logger.exception(e)

        metadata["retrieval_date"] = str(datetime.now())

        return metadata

    def _parse_review_text(self, text_block) -> str:
        text = ""
        for e, s in zip(text_block.contents, text_block.stripped_strings):
            if isinstance(e, Tag) and e.has_attr(
                "class"
            ):  #  and e.attrs["class"] in ["review-snippet","k8MTF",]:
                break
            text += s + " "

        text = re.sub("\s", " ", text)
        text = re.sub("'|\"", "", text)
        text = text.strip()
        return text

    def _handle_review_exception(self, result, review, name) -> dict:
        tb = re.sub("\s", " ", traceback.format_exc())
        tb = f"review {name}:{tb}"
        self.logger.error(tb)
        tb = re.sub("['\"]", " ", tb)
        result["errors"].append(tb)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
        with open(f"examples/error_{name}_{ts}.html", "w", encoding="utf-8") as f:
            f.writelines(str(review))
        return result

    def _parse_review(self, review: Tag):
        result = review_default_result.copy()

        # Parse text
        try:
            # Find text block
            text_block = review.find(True, class_="review-full-text")
            if not text_block:
                text_block = review.find(True, {"data-expandable-section": True})
            # Extract text
            if text_block:
                result["text"] = self._parse_review_text(text_block)
        except Exception as e:
            self._handle_review_exception(result, review, "text")

        # Parse review rating
        try:
            rating_text = review.find(True, class_="Fam1ne EBe2gf").get("aria-label")
            rating_text = re.sub(",", ".", rating_text)
            rating = re.findall("[0-9]+[.][0-9]*", rating_text)
            result["rating"] = float(rating[0])
            result["rating_max"] = float(rating[1])
        except Exception as e:
            self._handle_review_exception(result, review, "rating")

        # Parse other ratings
        try:
            other_ratings = review.find(True, class_="k8MTF")
            if other_ratings:
                s = " ".join([s for s in other_ratings.stripped_strings])
                result["other_ratings"] = re.sub("\s+", " ", s)
        except Exception as e:
            self._handle_review_exception(result, review, "other_ratings")

        # Parse relative date
        try:
            result["relative_date"] = review.find(True, class_="dehysf lTi8oc").text
        except Exception as e:
            self._handle_review_exception(result, review, "relative_date")

        # Parse user name
        try:
            result["user_name"] = review.find(True, class_="TSUbDb").text
        except Exception as e:
            self._handle_review_exception(result, review, "user_name")

        # Parse user metadata
        try:
            user_node = review.find(True, class_="Msppse")
            if user_node:
                result["user_url"] = user_node.get("href")
                result["user_is_local_guide"] = (
                    True if user_node.find(True, class_="QV3IV") else False
                )
                user_reviews = re.findall(
                    "[Uuma0-9.,]+(?= comentário| review)", user_node.text
                )
                user_photos = re.findall("[Uuma0-9.,]+(?= foto| photo)", user_node.text)
                if len(user_reviews) > 0:
                    result["user_reviews"] = user_reviews[0]
                if len(user_photos) > 0:
                    result["user_photos"] = user_photos[0]
        except Exception as e:
            self._handle_review_exception(result, review, "user_data")

        # Parse review id
        try:
            # result["review_id"] = review.find(True, {"data-ri": True}).get("data-ri")
            review_id = review.find(True, class_="RvU3D").get("href")
            result["review_id"] = re.findall("(?<=postId=).*?(?=&)", review_id)[0]
        except Exception as e:
            self._handle_review_exception(result, review, "review_id")

        # Parse review likes
        try:
            review_likes = review.find(True, jsname="CMh1ye")
            if review_likes:
                result["likes"] = int(review_likes.text)
        except Exception as e:
            self._handle_review_exception(result, review, "likes")

        # Parse review response
        try:
            response = review.find(True, class_="d6SCIc")
            if response:
                result["response_text"] = self._parse_review_text(response)
            response_date = review.find(True, class_="pi8uOe")
            if response_date:
                result["response_relative_date"] = response_date.text
        except Exception as e:
            self._handle_review_exception(result, review, "response")

        # Parse trip_type_travel_group
        try:
            trip_type_travel_group = review.find(True, class_="PV7e7")
            if trip_type_travel_group:
                s = " ".join([s for s in trip_type_travel_group.stripped_strings])
                result["trip_type_travel_group"] = re.sub("\s+", " ", s)
        except Exception as e:
            self._handle_review_exception(result, review, "trip_type_travel_group")

        # Make timestamp
        result["retrieval_date"] = str(datetime.now())

        return result

    def _node_to_xpath(self, node):
        node_type = {
            Tag: getattr(node, "name"),
            Comment: "comment()",
            NavigableString: "text()",
        }
        same_type_siblings = list(
            node.parent.find_all(
                lambda x: getattr(node, "name", True) == getattr(x, "name", False),
                recursive=False,
            )
        )
        if len(same_type_siblings) <= 1:
            return node_type[type(node)]
        pos = same_type_siblings.index(node) + 1
        return f"{node_type[type(node)]}[{pos}]"

    def _get_node_xpath(self, node: Union[Tag, Comment]):
        xpath = "/"
        elements = [f"{self._node_to_xpath(node)}"]
        for p in node.parents:
            if p.name == "[document]":
                break
            elements.insert(0, self._node_to_xpath(p))

        xpath = "/" + xpath.join(elements)
        return xpath

    def scrape_reviews(
        self,
        url: str,
        writer,
        file,
        n_reviews: int,
        hl: str = "",
        sort_by: str = "",
        token: str = "",
    ):
        url_name = re.findall("(?<=place/).*?(?=/)", url)[0]
        url_name = urllib.parse.unquote_plus(url_name)
        self.logger.info(f"Scraping url: {url_name}")

        feature_id = self._parse_url_to_feature_id(url)
        sort_by_id = self._parse_sort_by(sort_by)

        results = []
        j = 0

        n_requests = math.ceil((n_reviews) / 10)
        for i in range(n_requests):
            self.logger.info(f"Request: {i:>8}; review: {j:>8}")
            n = self.n_retries
            while n > 0:
                try:
                    _, reviews_soup, review_count, token = self._get_api_response(
                        feature_id,
                        hl=hl,
                        sort_by_id=sort_by_id,
                        token=token,
                    )
                    break
                except Exception as e:
                    self.logger.error(f"error on request: {i}")
                    self.logger.exception(e)
                    self.logger.info(f"waiting {self.retry_time} seconds")
                    time.sleep(self.retry_time)
                    n -= 1

            try:
                for review in reviews_soup:
                    # self.logger.info(f"Parsing review: {j:>8}")
                    result = self._parse_review(review)
                    result["token"] = token

                    writer.writerow(result.values())
                    file.flush()

                    results.append(result)
                    # self.logger.info(results)
                    j += 1
            except Exception as e:
                self.logger.error(f"error parsing request: {i}")
                self.logger.exception(e)

            if review_count < 10 or token == "":
                self.logger.info(f"Place review limit at {j}")
                break

            # Waiting so google wont block this scraper
            time.sleep(self.request_interval)

        self.logger.info(
            f"Done Scraping Reviews\nRequests made: {i+1}\nReviews parsed: {j}"
        )

        return results

    def scrape_place(
        self,
        url: str,
        writer,
        file,
        hl: str = "",
    ):
        url_name = re.findall("(?<=place/).*?(?=/)", url)[0]
        url_name = urllib.parse.unquote_plus(url_name)
        self.logger.info(f"Scraping url: {url_name}")

        feature_id = self._parse_url_to_feature_id(url)

        self.logger.info(f"Parsing metadata...")
        response_soup, _, _, _ = self._get_api_response(
            feature_id,
            hl=hl,
        )
        metadata = self._parse_place(response=response_soup)
        metadata["feature_id"] = feature_id
        metadata["url"] = url

        writer.writerow(metadata.values())
        file.flush()

        return metadata
