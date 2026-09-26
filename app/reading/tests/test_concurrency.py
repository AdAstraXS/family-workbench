from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from django.db import connection, connections
from django.test import Client, TransactionTestCase
from django.urls import reverse
from reading.models import ReadingPosition, ReadingArtifactVersion, Book
from reading.services import process_file
from reading.artifacts import publish, validate_data, export_html
from .test_reading import ReadingTests
from .test_collaboration import structured


@skipUnless(connection.vendor=="postgresql","Requires disposable PostgreSQL for real concurrent transactions")
class ConcurrentReadingTests(TransactionTestCase):
    setUp=ReadingTests.setUp
    member=ReadingTests.member
    upload=ReadingTests.upload
    payload=ReadingTests.payload

    def test_two_clients_creating_same_position_return_one_conflict(self):
        book=self.upload();process_file(book.file.pk);payload=self.payload(book)
        clients=[Client(),Client()]
        for client in clients:client.force_login(self.owner.user)
        barrier=Barrier(2)
        def save(client):
            try:
                barrier.wait(timeout=10)
                return client.post(reverse("reading:position",args=[book.pk]),payload,content_type="application/json").status_code
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:codes=list(pool.map(save,clients))
        self.assertEqual(sorted(codes),[200,409]);self.assertEqual(ReadingPosition.objects.count(),1)

    def test_concurrent_identical_publication_creates_one_version(self):
        book=self.upload();data=validate_data(structured());raw=export_html(data);barrier=Barrier(2)
        def save(_):
            try:
                barrier.wait(timeout=10)
                return publish(book,self.owner,data["title"],Book.PRIVATE,raw,data)[0].pk
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:ids=list(pool.map(save,range(2)))
        self.assertEqual(ids[0],ids[1]);self.assertEqual(ReadingArtifactVersion.objects.count(),1)
